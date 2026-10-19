#!/usr/bin/env python3
"""Build the small native package set and publish an authenticated pacman repo.

The build step runs makepkg as an unprivileged user in a native aarch64 Arch
Linux ARM build root, without access to the signing home. The assemble step
signs a custom package only if its bytes and SHA-256 match the
build-record.json that the build step wrote for the current locks.
The output includes public keys and signed packages only; GNUPGHOME stays private.
"""
from __future__ import annotations
import argparse,json,os,platform,shutil,subprocess,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from h7607a.artifacts import fetch,verify
from h7607a.packages import ROOT,PackageError,require,run,pin,package_plan,package_metadata,signature,verify_key,verify_ubuntu_sources,repository_filename

def link_file(src,dst):
    require(not dst.exists()and not dst.is_symlink(),'Refusing to overwrite existing repository member '+str(dst))
    try:os.link(src,dst)
    except OSError:shutil.copyfile(src,dst)

def sign(path,home,fingerprint):
    run(['gpg','--homedir',home,'--batch','--yes','--local-user',fingerprint,'--output',str(path)+'.sig','--detach-sign',path])

def prepare_inputs(root,work,package,artifacts):
    recipe=root/package['recipe'];require(recipe.is_dir(),'Package recipe absent: '+package['name'])
    work.mkdir(mode=0o700)
    for source in recipe.iterdir():
        require(source.is_file()and not source.is_symlink(),'Recipe has unexpected nested file')
        shutil.copy2(source,work/source.name)
    spec=json.loads((recipe/'inputs.json').read_text());require(spec['package']==package['name'],'Package input owner differs')
    for entry in spec['inputs']:
        name=entry['filename'];require(Path(name).name==name,'Package input filename escapes workdir')
        (work/name).symlink_to(verify(root,artifacts[entry['artifact_id']]).resolve())
    return work

def build(root,output,work,offline,cache_dirs,only):
    lock,artifacts,packages,lock_hashes=package_plan(root)
    keys={k:verify_key(root,v)for k,v in lock['signing_keys'].items()}
    needed=set(lock['ubuntu_archive']['deb_ids'])|set(lock['ubuntu_archive'][k]for k in ('keyring_id','inrelease_id','index_id'))
    for p in packages.values():
        if p['kind']=='custom':
            spec=json.loads((root/p['recipe']/'inputs.json').read_text());needed.update(i['artifact_id']for i in spec['inputs'])
        else:needed.update((p['artifact_id'],p['signature_id']))
    for p in lock.get('provenance_packages',[]):needed.update((p['artifact_id'],p['signature_id']))
    for i in sorted(needed):fetch(root,artifacts[i],cache_dirs=cache_dirs,offline=offline)
    verify_ubuntu_sources(root,lock,artifacts)
    for p in list(packages.values())+lock.get('provenance_packages',[]):
        if p.get('kind')=='custom':continue
        signature(verify(root,artifacts[p['artifact_id']]),verify(root,artifacts[p['signature_id']]),keys['archlinuxarm'],lock['signing_keys']['archlinuxarm']['fingerprint'])
    require(platform.machine()in ('aarch64','arm64')and sys.platform=='linux','Native ARM Linux is required for makepkg')
    require(os.geteuid()!=0,'Run makepkg as an unprivileged build user')
    for command in ('makepkg','bsdtar','zstd','meson','ninja','pkg-config','gpg','repo-add'):require(shutil.which(command),'Missing build tool '+command)
    output.mkdir(parents=True,exist_ok=True);work.mkdir(parents=True,exist_ok=True)
    records=[]
    for p in packages.values():
        if p['kind']!='custom':continue
        if only and p['name']not in only:continue
        print('Building '+p['name']+' '+p['version'],flush=True)
        directory=work/p['name'];prepare_inputs(root,directory,p,artifacts)
        env=dict(os.environ,PKGDEST=str(output.resolve()),SRCDEST=str(directory.resolve()),BUILDDIR=str(directory.resolve()),PKGEXT='.pkg.tar.zst',COMPRESSZST='(zstd -c -T0 -3 -)')
        completed=subprocess.run(['makepkg','--nodeps','--cleanbuild','--force','--noconfirm'],cwd=directory,env=env)
        require(completed.returncode==0,'Native makepkg failed: '+p['name'])
        matches=[]
        for f in output.glob(p['name']+'-*.pkg.tar.*'):
            if f.name.endswith('.sig'):continue
            if package_metadata(f)=={'pkgname':p['name'],'pkgver':p['version'],'arch':p.get('architecture','aarch64')}:matches.append(f)
        require(len(matches)==1,'Built package identity missing or ambiguous: '+p['name'])
        records.append(dict(name=p['name'],version=p['version'],file=matches[0].name,**pin(matches[0])))
        shutil.rmtree(directory)
    (output/'build-record.json').write_text(json.dumps(dict(schema='h7607a-native-package-build-v1',lock_sha256=lock_hashes,packages=records),indent=2)+'\n')

def recorded_packages(built,packages,lock_hashes):
    """Return {name: (file, pin)} for every custom package the build step recorded against these locks."""
    path=built/'build-record.json';require(path.is_file()and not path.is_symlink(),'Build record absent: '+str(path))
    record=json.loads(path.read_text());require(record.get('schema')=='h7607a-native-package-build-v1','Unknown build record schema')
    require(record.get('lock_sha256')==lock_hashes,'Build record was written for different locks')
    rows={r['name']:r for r in record['packages']};require(len(rows)==len(record['packages']),'Duplicate build record entry')
    found={}
    for p in packages.values():
        if p['kind']!='custom':continue
        row=rows.get(p['name']);require(row is not None and row['version']==p['version'],'Build record lacks '+p['name']+' '+p['version'])
        src=built/row['file'];require(Path(row['file']).name==row['file']and row['file']not in ('.','..'),'Unsafe build record filename: '+p['name'])
        require(src.is_file()and not src.is_symlink()and src.stat().st_size==row['bytes'],'Built package differs from build record: '+p['name'])
        found[p['name']]=(src,{'bytes':row['bytes'],'sha256':row['sha256']})
    return found

def assemble(root,built,repo,signing_home):
    lock,artifacts,packages,lock_hashes=package_plan(root);keys={k:verify_key(root,v)for k,v in lock['signing_keys'].items()}
    recorded=recorded_packages(built,packages,lock_hashes)
    require(repo.name==lock['local_repository_path'].split('/')[-1],'Unexpected repository destination basename')
    require(not repo.exists(),'Publish only into an absent repository directory')
    repo.parent.mkdir(parents=True,exist_ok=True);repo.mkdir(mode=0o755)
    fp=lock['signing_keys']['local']['fingerprint'];records=[]
    for p in packages.values():
        if p['kind']=='custom':
            src,expected=recorded[p['name']];dst=repo/repository_filename(src.name);shutil.copyfile(src,dst)
            # Hash the copy: the build user can still replace files in its output directory.
            require(pin(dst)==expected,'Built package differs from build record: '+p['name'])
        else:
            src=verify(root,artifacts[p['artifact_id']]);sig=verify(root,artifacts[p['signature_id']]);dst=repo/repository_filename(src.name);signature(src,sig,keys['archlinuxarm'],lock['signing_keys']['archlinuxarm']['fingerprint']);link_file(src,dst);link_file(sig,repo/(dst.name+'.sig'))
        meta=package_metadata(dst);require(meta=={'pkgname':p['name'],'pkgver':p['version'],'arch':p.get('architecture','aarch64')},'Package metadata differs before indexing')
        if p['kind']=='custom':sign(dst,signing_home,fp)
        records.append(dict(name=p['name'],version=p['version'],file=dict(path=dst.name,**pin(dst)),signature=dict(path=dst.name+'.sig',**pin(repo/(dst.name+'.sig')))))
    database=repo/(lock['repository']+'.db.tar.gz')
    run(['repo-add',database]+[repo/r['file']['path']for r in records])
    manifest=dict(schema='h7607a-signed-package-repository-v1',repository=lock['repository'],lock_sha256=lock_hashes,packages=records)
    for role,suffix in [('database','db'),('files_database','files')]:
        path=repo/(lock['repository']+'.'+suffix+'.tar.gz');require(path.is_file(),'repo-add omitted database');sign(path,signing_home,fp)
        manifest[role]=dict(path=path.name,**pin(path));manifest[role+'_signature']=dict(path=path.name+'.sig',**pin(repo/(path.name+'.sig')))
        alias=repo/(lock['repository']+'.'+suffix+'.sig');alias.symlink_to(path.name+'.sig')
    out=repo/'repository.json';out.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n');sign(out,signing_home,fp)
    print(json.dumps(dict(repository=str(repo),packages=len(records))))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source-root',type=Path,default=ROOT);ap.add_argument('--output',type=Path);ap.add_argument('--work',type=Path);ap.add_argument('--repo',type=Path);ap.add_argument('--signing-home',type=Path);ap.add_argument('--offline',action='store_true');ap.add_argument('--cache-dir',type=Path,action='append',default=[]);ap.add_argument('--build-only',action='store_true');ap.add_argument('--assemble-only',action='store_true');ap.add_argument('--only',action='append',default=[])
    args=ap.parse_args();root=args.source_root.resolve();output=(args.output or root/'.work/package-output').resolve();work=(args.work or root/'.work/package-build').resolve();repo=(args.repo or root/'build/repo').resolve()
    require(not(args.build_only and args.assemble_only),'Select one build mode')
    require(not args.only or args.build_only,'Partial builds cannot publish a repository')
    if not args.assemble_only:build(root,output,work,args.offline,args.cache_dir,args.only)
    if not args.build_only:
        require(args.signing_home is not None,'Explicit private signing home required');assemble(root,output,repo,args.signing_home.resolve())
if __name__=='__main__':
    try:main()
    except (PackageError,RuntimeError)as exc:raise SystemExit(str(exc))
