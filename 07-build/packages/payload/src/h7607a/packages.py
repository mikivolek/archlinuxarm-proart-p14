"""Construct signed pacman repositories and configure their package sources."""
from __future__ import annotations
import hashlib,json,lzma,os,re,subprocess,sys,tempfile,urllib.parse
from pathlib import Path
from .artifacts import load_lock,verify,verify_file
ROOT=Path(__file__).resolve().parents[2]
class PackageError(RuntimeError):pass
def require(value,message):
    if not value:raise PackageError(message)
def run(argv,**kw):
    result=subprocess.run([str(x)for x in argv],check=False,capture_output=True,text=True,**kw)
    require(result.returncode==0,'Command failed: '+str(argv[0])+': '+result.stderr[-3000:])
    return result.stdout

def digest(path):
    with Path(path).open('rb')as f:return hashlib.file_digest(f,'sha256').hexdigest()

def pin(path):return {'bytes':Path(path).stat().st_size,'sha256':digest(path)}

def repository_filename(name):
    """Cache names may be URL-escaped; file:// repository names must not be."""
    decoded=urllib.parse.unquote(name,errors='strict')
    require(bool(re.fullmatch(r'[A-Za-z0-9._+:~\-]+',decoded))and decoded not in ('.','..'),'Unsafe repository package filename')
    return decoded

def archive(path,operation,member=None,*,binary=False):
    args=['bsdtar',operation,str(path)]+([member]if member else[])
    # Apple's libarchive lacks built-in zstd; native ALARM supports it directly.
    if sys.platform=='darwin'and str(path).endswith('.zst'):
        producer=subprocess.Popen(['zstd','-q','-d','-c',str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            result=subprocess.run(['bsdtar',operation,'-']+([member]if member else[]),stdin=producer.stdout,capture_output=True)
            producer.stdout.close();producer.communicate();require(producer.returncode in (0,-13),'Zstd package decoder failed')
        finally:
            if producer.poll()is None:producer.kill();producer.wait()
    else:result=subprocess.run(args,capture_output=True)
    require(result.returncode==0,'Cannot read package archive: '+result.stderr.decode(errors='replace')[-1000:])
    return result.stdout if binary else result.stdout.decode()

def signature(data,signature_path,keyring,fingerprint):
    require(bool(re.fullmatch(r'[0-9A-F]{40}',fingerprint)),'Invalid signing fingerprint')
    with tempfile.TemporaryDirectory(prefix='h7607a-gpg-')as home:
        output=run(['gpgv','--homedir',home,'--status-fd','1','--keyring',Path(keyring).resolve(),signature_path,data])
    valid=[]
    for line in output.splitlines():
        fields=line.split()
        if len(fields)>2 and fields[:2]==['[GNUPG:]','VALIDSIG']:valid.append(fields[2])
    require(valid==[fingerprint],'Detached signature signer differs from pinned key')

def verify_key(root,row):
    p=verify(root,row)
    output=run(['gpg','--batch','--with-colons','--show-keys',p]);primaries=[];want=False
    for line in output.splitlines():
        fields=line.split(':')
        if fields[0]=='pub':want=True
        elif fields[0]=='sub':want=False
        elif fields[0]=='fpr'and want:primaries.append(fields[9]);want=False
    require(primaries==[row['fingerprint']],'Keyring has another or extra primary identity')
    return p

def package_metadata(path):
    text=archive(path,'-xOf','.PKGINFO');meta={}
    for line in text.splitlines():
        if ' = 'in line:
            key,value=line.split(' = ',1);meta.setdefault(key,[]).append(value)
    require(all(len(meta.get(key,[]))==1 for key in ('pkgname','pkgver','arch')),'Package identity is missing or ambiguous')
    return {key:meta[key][0]for key in ('pkgname','pkgver','arch')}

def package_plan(source_root=ROOT):
    root=Path(source_root);lock=load_lock(root/'locks/packages.json')
    artifacts={r['id']:r for r in lock['artifacts']};wanted={r['name']:r for r in lock['packages']}
    lock_hashes={'locks/packages.json':digest(root/'locks/packages.json')}
    for name in lock.get('additional_locks',[]):
        other=load_lock(root/name);lock_hashes[name]=digest(root/name)
        for row in other['artifacts']:
            require(row['id']not in artifacts or artifacts[row['id']]==row,'Artifact ID collision across locks')
            artifacts[row['id']]=row
    for package in wanted.values():
        for path,expected in package.get('recipe_files',{}).items():verify_file(root/path,{'path':path,**expected})
    return lock,artifacts,wanted,lock_hashes

def verify_ubuntu_sources(root,lock,artifacts):
    """Authenticate the historical binary inputs through Ubuntu's signed index.

    The retained InRelease is a provenance record for fixed historical binaries,
    not a current apt repository; its hash and exact archive signer are pinned.
    """
    spec=lock['ubuntu_archive'];paths={k:verify(root,artifacts[spec[k]])for k in ('keyring_id','inrelease_id','index_id')}
    with tempfile.TemporaryDirectory(prefix='h7607a-ubuntu-')as home:
        status=run(['gpgv','--homedir',home,'--status-fd','1','--keyring',paths['keyring_id'],paths['inrelease_id']])
    valid=[l.split()[2]for l in status.splitlines()if l.startswith('[GNUPG:] VALIDSIG ')]
    require(valid==[spec['fingerprint']],'Ubuntu InRelease signer differs')
    release=paths['inrelease_id'].read_text();section=release.split('\nSHA256:\n',1)[1].split('\nSHA512:',1)[0]
    matches=[line.split()for line in section.splitlines()if len(line.split())==3 and line.split()[2]==spec['index_path']]
    require(len(matches)==1,'Signed Ubuntu index digest missing or ambiguous')
    sha,size,_=matches[0];verify_file(paths['index_id'],{'path':paths['index_id'].name,'sha256':sha,'bytes':int(size)})
    paragraphs=lzma.decompress(paths['index_id'].read_bytes()).decode().split('\n\n');stanzas=[]
    for paragraph in paragraphs:
        fields={}
        for line in paragraph.splitlines():
            if ': 'in line and not line.startswith(' '):
                k,v=line.split(': ',1);fields[k]=v
        stanzas.append(fields)
    for artifact_id in spec['deb_ids']:
        row=artifacts[artifact_id];deb=verify(root,row)
        matches=[s for s in stanzas if Path(s.get('Filename','')).name==deb.name]
        require(len(matches)==1,'Ubuntu binary absent or ambiguous in signed index')
        item=matches[0];require(item['SHA256']==row['sha256']and int(item['Size'])==row['bytes'],'Ubuntu package differs from signed index')
    return {'signer':spec['fingerprint'],'binary_count':len(spec['deb_ids'])}

def configure_live(repo_root,*,root=Path('/'),config=Path('/etc/pacman.conf'),source_root=ROOT):
    """Configure a file repository with pacman's package/database signature policy."""
    source_root=Path(source_root).resolve()
    lock=load_lock(source_root/'locks/packages.json')
    value=json.loads((Path(repo_root)/'repository.json').read_text())
    root=Path(root).resolve()
    config=root/Path(config).relative_to('/')
    gpgdir=root/'etc/pacman.d/gnupg'
    public=source_root/lock['signing_keys']['local']['path']
    fingerprint=lock['signing_keys']['local']['fingerprint']
    run(['pacman-key','--gpgdir',gpgdir,'--add',public])
    run(['pacman-key','--gpgdir',gpgdir,'--lsign-key',fingerprint])

    # Replace every occurrence by repository identifier, independent of comments.
    # Remove live-image upgrade holds; installed-system holds are set separately.
    lines=[];skip=False;removed_holds=[]
    for line in config.read_text().splitlines():
        section=re.match(r'^\s*\[([^]]+)\]\s*(?:#.*)?$',line)
        if section:skip=section[1]==lock['repository']
        if skip or re.match(r'^\s*#\s*(?:BEGIN|END) H7607A (?:VERIFIED )?LOCAL REPOSITORY\s*$',line):continue
        if re.match(r'^\s*(?:IgnorePkg|IgnoreGroup)\s*=',line):
            removed_holds.append(line);continue
        lines.append(line)
    old='\n'.join(lines).rstrip()+'\n'
    block=f'[{lock["repository"]}]\nSigLevel = Required DatabaseRequired\nServer = {Path(repo_root).resolve().as_uri()}\n\n'
    match=re.search(r'^\s*\[(?!options\])[^]]+\]\s*(?:#.*)?$',old,re.M)
    offset=match.start()if match else len(old)
    new=old[:offset]+block+old[offset:]
    fd,tmp=tempfile.mkstemp(prefix='.h7607a-pacman-',dir=config.parent)
    try:
        with os.fdopen(fd,'w')as f:f.write(new);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,config.stat().st_mode&0o777);os.replace(tmp,config)
    finally:Path(tmp).unlink(missing_ok=True)
    return {'repository':lock['repository'],'packages':{r['name']:r['version']for r in value['packages']},'config':str(config),'local_key_fingerprint':fingerprint,'signatures_required':True,'removed_live_holds':removed_holds}
