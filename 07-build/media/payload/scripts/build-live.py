#!/usr/bin/env python3
"""Construct H7607BA ext4 and FAT32 installer images on native ARM64 Linux."""
import argparse,hashlib,json,os,platform,re,shutil,subprocess,sys,uuid,tarfile
from pathlib import Path
sys.dont_write_bytecode=True
REPO=Path(__file__).resolve().parents[1]
WORK=Path('/var/tmp/h7607a-live-build')
TMPFS_BYTES=8*1024**3
CHUNK=1024*1024

def need(value,message):
 if not value:raise RuntimeError(message)
def run(argv,**kw):
 return subprocess.run([str(x) for x in argv],check=True,**kw)
def digest(path):
 with Path(path).open('rb')as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(path,value):
 temp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
 with temp.open('x')as f:json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
 temp.chmod(0o600);os.replace(temp,path)
def lock():
 value=json.loads((REPO/'locks/live-base.json').read_text());need(value['schema']=='h7607a-live-base-v2','Unknown source lock');return value

def memory_available():
 rows=dict(line.split(':',1)for line in Path('/proc/meminfo').read_text().splitlines());return int(rows['MemAvailable'].split()[0])*1024

def mount_info(path):
 p=subprocess.run(['findmnt','--json','--mountpoint',str(path),'-o','TARGET,SOURCE,FSTYPE,OPTIONS'],capture_output=True,text=True)
 if p.returncode==1:return None
 need(p.returncode==0,'findmnt failed');rows=json.loads(p.stdout).get('filesystems',[]);need(len(rows)==1,'Ambiguous mount');return rows[0]


def checked_work():
 need(WORK.is_dir() and not WORK.is_symlink(),'Private work directory absent')
 m=mount_info(WORK);need(m and m['fstype']=='tmpfs' and 'noswap'in m['options'].split(','),'Build workspace must be no-swap tmpfs')
 state=json.loads((WORK/'state.json').read_text());need(state['schema']=='h7607a-live-build-v1','Unknown build ownership')
 need(state['source_lock_sha256']==digest(REPO/'locks/live-base.json'),'Live source lock changed')
 return state

def empty_image(destination,size):
 """Create a new sparse filesystem image without accepting existing state."""
 need(type(size)is int and size>0 and size%512==0,'Invalid image size')
 fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
 try:
  os.ftruncate(fd,size);os.fsync(fd)
 finally:os.close(fd)

def verify_base(value):
 sys.path.insert(0,str(REPO/'src'));from h7607a.artifacts import verify
 from h7607a.packages import signature
 rows={row['path']:row for row in value['artifacts']}
 need(len(rows)==len(value['artifacts'])==2 and set(rows)=={value['source'],value['signature']},'Rootfs artifact set differs')
 source=verify(REPO,rows[value['source']]);sig=verify(REPO,rows[value['signature']])
 need(rows[value['source']]['sha256']==value['source_sha256']and rows[value['source']]['bytes']==value['source_bytes'],'Rootfs lock identity differs')
 key=REPO/value['keyring'];need(digest(key)==value['keyring_sha256'],'Rootfs key differs')
 signature(source,sig,key,value['signer'])
 return source,{'rootfs_sha256':value['source_sha256'],'rootfs_bytes':value['source_bytes'],'signer':value['signer'],'fresh_filesystem':True}

def bootstrap_chroot():
 """Read the host helper directly from a locked, signed ALARM package."""
 spec=json.loads((REPO/'locks/live-tools.json').read_text())
 rows=[row for row in spec['packages']if row['name']=='arch-install-scripts']
 need(len(rows)==1,'Pinned arch-install-scripts bootstrap package required')
 row=rows[0];verify_live_package(row,spec,verify_signature=True)
 with tarfile.open(REPO/row['path'],'r:*')as archive:
  members=[m for m in archive if m.name.lstrip('./')=='usr/bin/arch-chroot']
  need(len(members)==1 and members[0].isfile() and 0<members[0].size<262144,'Invalid arch-chroot package member')
  data=archive.extractfile(members[0]).read()
 need(data.startswith(b'#!/bin/bash\n'),'Unexpected arch-chroot interpreter')
 tools=WORK/'tools';tools.mkdir(mode=0o700);helper=tools/'arch-chroot'
 with helper.open('xb')as stream:stream.write(data)
 helper.chmod(0o755)
 return {'package_sha256':row['sha256'],'helper_sha256':hashlib.sha256(data).hexdigest()}

def prepare():
 value=lock();source,proof=verify_base(value)
 need(memory_available()>=8500*1024**2,'Need at least8500MiB available VM memory before rootfs extraction')
 need(not WORK.exists(),'Build workspace already exists; inspect or clean it explicitly')
 WORK.mkdir(mode=0o700);run(['mount','-t','tmpfs','-o',f'size={TMPFS_BYTES},mode=0700,nodev,nosuid,noswap','h7607a-live-build',WORK])
 # Do this before any image mount: otherwise host service namespaces retain
 # propagated copies after the main namespace unmounts the build root.
 run(['mount','--make-private',WORK])
 state={'schema':'h7607a-live-build-v1','phase':'PREPARING','source_lock_sha256':digest(REPO/'locks/live-base.json'),'source_sha256':value['source_sha256'],'loop':None,'physical_disk_accessed':False};save(WORK/'state.json',state)
 empty_image(WORK/'root.img',value['root_bytes'])
 run(['mkfs.ext4','-F','-U',value['root_uuid'],'-L',value['root_label'],'-E','lazy_itable_init=0,lazy_journal_init=0',WORK/'root.img'])
 loop=run(['losetup','--find','--show','--nooverlap',WORK/'root.img'],capture_output=True,text=True).stdout.strip()
 need(re.fullmatch('/dev/loop[0-9]+',loop),'Unexpected loop device')
 state.update(loop=loop,source_proof=proof);save(WORK/'state.json',state)
 root=WORK/'root';root.mkdir(mode=0o700);run(['mount','-t','ext4','-o','noatime',loop,root])
 need(mount_info(root)['source']==loop,'Prepared root mount differs')
 run(['tar','--numeric-owner','--acls','--xattrs','-xpf',source,'-C',root])
 state['bootstrap_chroot']=bootstrap_chroot();state['phase']='PREPARED';save(WORK/'state.json',state)
 ensure_build_access(root)
 print(json.dumps({'phase':'PREPARED','root':str(root),'loop':loop,'source_proof':proof,'available_memory_bytes':memory_available()}),flush=True)


def mounted_root(state):
 root=WORK/'root';m=mount_info(root)
 need(m and m['fstype']=='ext4' and m['source']==state['loop'],'Owned root mount differs')
 ensure_build_access(root)
 return root

def ensure_build_access(root):
 tools=WORK/'tools';helper=tools/'arch-chroot';state=json.loads((WORK/'state.json').read_text())
 need(helper.is_file()and not helper.is_symlink()and digest(helper)==state['bootstrap_chroot']['helper_sha256'],'Pinned build helper changed')
 installed=root/'usr/bin/arch-chroot'
 if installed.exists():need(digest(installed)==digest(helper),'Installed build helper differs from bootstrap package')
 os.environ['PATH']=str(tools)+os.pathsep+os.environ.get('PATH','/usr/sbin:/usr/bin:/sbin:/bin')
 source=root/'source';source.mkdir(exist_ok=True,mode=0o755)
 if not mount_info(source):
  need(not any(source.iterdir()),'Build source bind destination is not empty')
  run(['mount','--bind',REPO,source]);run(['mount','-o','remount,bind,ro',source])
 info=mount_info(source)
 need(info and 'ro'in info['options'].split(',')and os.path.samefile(source,REPO),'Exact read-only repository bind required')

def chroot(root,argv,**kw):return run(['arch-chroot',root,*argv],**kw)

def remove_path(root,relative):
 path=root/relative
 need(not Path(relative).is_absolute() and '..'not in Path(relative).parts,'Unsafe cleanup path')
 need(path.parent.resolve().is_relative_to(root.resolve()),'Cleanup path escapes live root')
 if path.is_symlink()or path.is_file():path.unlink()
 elif path.is_dir():
  mounts=json.loads(run(['findmnt','--json','--list','-o','TARGET'],capture_output=True,text=True).stdout)['filesystems']
  need(not any(Path(m['target']).is_relative_to(path)for m in mounts),'Refuse cleanup across mounted filesystem: '+relative)
  shutil.rmtree(path)

def write_text(root,relative,text,mode=0o644):
 path=root/relative;need(path.parent.resolve().is_relative_to(root.resolve()),'Target path escapes live root')
 path.parent.mkdir(parents=True,exist_ok=True)
 need(not path.is_symlink(),'Refuse symlink target '+relative);path.write_text(text);path.chmod(mode)

def verify_live_package(row,spec,*,verify_signature=False):
 sys.path.insert(0,str(REPO/'src'));from h7607a.artifacts import verify
 from h7607a.packages import signature
 for field,sha in [('path','sha256'),('signature_path','signature_sha256')]:
  verify(REPO,{'path':row[field],'bytes':row[field+'_bytes'],'sha256':row[sha]})
 if verify_signature:
  key=REPO/spec['keyring'];need(digest(key)==spec['keyring_sha256'],'Live tools key changed')
  signature(REPO/row['path'],REPO/row['signature_path'],key,spec['signer'])

def remove_generic_packages(root,spec):
 # Remove the upstream generic kernel and unrelated firmware through pacman.
 # The dedicated platform kernel and exact device firmware arrive in provision.
 installed=set(chroot(root,['pacman','-Qq'],capture_output=True,text=True).stdout.splitlines())
 retain={'linux-firmware-mediatek','linux-firmware-cirrus','linux-firmware-whence'}
 remove=sorted(n for n in installed if n=='linux-aarch64'or(n.startswith('linux-firmware')and n not in retain))
 if remove:chroot(root,['pacman','-R','--noconfirm',*remove])
 return remove

def live_tools():
 state=checked_work()
 root=mounted_root(state);spec=json.loads((REPO/'locks/live-tools.json').read_text())
 paths=[]
 for row in spec['packages']:
  verify_live_package(row,spec);paths.append('/source/'+row['path'])
 chroot(root,['pacman-key','--init'])
 chroot(root,['pacman-key','--populate','archlinuxarm'])
 state['removed_generic_packages']=remove_generic_packages(root,spec)
 chroot(root,['pacman','-U','--needed','--noconfirm',*paths])
 state['live_tools_lock_sha256']=digest(REPO/'locks/live-tools.json');save(WORK/'state.json',state)
 print('Installed '+str(len(paths))+' pinned ALARM packages.',flush=True)

def create_esp(root,state):
 path=WORK/'ESP.img';esp=root/'boot';value=lock()
 if 'esp_loop'in state:
  m=mount_info(esp);need(m and m['source']==state['esp_loop']and m['fstype']=='vfat','Owned ESP mount changed');return
 need(not path.exists()and not mount_info(esp),'Fresh ESP image required')
 remove_path(root,'boot');esp.mkdir(mode=0o700)
 with path.open('xb')as f:f.truncate(value['ESP_bytes'])
 run(['mkfs.vfat','-F','32','-n','N1XESP','-i',value['ESP_uuid'].replace('-',''),path])
 loop=run(['losetup','--find','--show','--nooverlap',path],capture_output=True,text=True).stdout.strip();need(re.fullmatch('/dev/loop[0-9]+',loop),'Unexpected ESP loop')
 state['esp_loop']=loop;save(WORK/'state.json',state)
 run(['mount','-t','vfat','-o','umask=0077',loop,esp])

def configure_live_console(root):
 # The upstream rootfs selects LANG=C. systemd-vconsole-setup uses the locale
 # to choose the kernel console decoder; Python's UTF-8 coercion alone cannot
 # correct that decoder. C.UTF-8 is built into the pinned glibc.
 write_text(root,'etc/locale.conf','LANG=C.UTF-8\n')
 write_text(root,'etc/vconsole.conf','KEYMAP=uk\n')


def hygiene(root):
 for relative in ('root/.ssh','root/.bash_history','root/.cache','var/lib/NetworkManager','var/lib/bluetooth','var/lib/systemd/random-seed'):
  remove_path(root,relative)
 for directory in ('var/log','var/tmp','tmp','var/cache/pacman/pkg'):
  for path in (root/directory).iterdir():remove_path(root,str(path.relative_to(root)))
 for path in (root/'etc/ssh').glob('ssh_host_*'):remove_path(root,str(path.relative_to(root)))
 for path in (root/'etc/NetworkManager/system-connections').glob('*'):remove_path(root,str(path.relative_to(root)))
 for name in ('sshd.service','bluetooth.service','NetworkManager-wait-online.service','wpa_supplicant.service','iwd.service','dhcpcd.service',
              'systemd-networkd.service','systemd-networkd-wait-online.service','systemd-networkd.socket',
              'systemd-networkd-varlink.socket','systemd-networkd-varlink-metrics.socket','systemd-networkd-resolve-hook.socket'):
  if(root/'usr/lib/systemd/system'/name).exists():run(['systemctl','--root',root,'disable',name],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 chroot(root,['passwd','-d','root'],stdout=subprocess.PIPE)
 # Remove the default upstream account; this live installer has root autologin.
 chroot(root,['userdel','-r','alarm'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 config=(root/'etc/pacman.conf').read_text()
 config=re.sub(r'^\[aur\]\s*\n.*?(?=^\[|\Z)','',config,flags=re.M|re.S)
 write_text(root,'etc/pacman.conf',config)
 write_text(root,'etc/hostname','h7607a-live\n')
 configure_live_console(root)
 write_text(root,'etc/systemd/journald.conf.d/60-h7607a-installer.conf','[Journal]\nStorage=persistent\nSystemMaxUse=128M\nSystemKeepFree=512M\nMaxRetentionSec=7day\n')
 write_text(root,'etc/NetworkManager/conf.d/10-dns.conf','[main]\ndns=systemd-resolved\n')
 remove_path(root,'etc/resolv.conf');(root/'etc/resolv.conf').symlink_to('../run/systemd/resolve/stub-resolv.conf')
 write_text(root,'etc/machine-id','')
 write_text(root,'etc/motd','ASUS ProArt P16 H7607BA - Arch Linux ARM installer\n\nConnect networking with nmtui if needed, then run: archinstall\nThe guided installer creates a fresh system on the internal SSD.\n')
 write_text(root,'etc/systemd/system/getty@tty1.service.d/installer.conf','[Service]\nExecStart=\nExecStart=-/usr/bin/agetty --autologin root --noclear %I $TERM\n')
 write_text(root,'etc/fstab',f"UUID={lock()['root_uuid']} / ext4 defaults,noatime 0 1\nUUID={lock()['ESP_uuid']} /boot vfat defaults,umask=0077 0 2\n")
 run(['systemctl','--root',root,'set-default','multi-user.target'])
 run(['systemctl','--root',root,'enable','NetworkManager.service','systemd-resolved.service','systemd-timesyncd.service','n1x-cppc-init.service','power-profiles-daemon.service'])
 run(['systemctl','--root',root,'disable','NetworkManager-wait-online.service'])


def provision():
 state=checked_work()
 root=mounted_root(state);need(memory_available()>1400*1024**2,'Insufficient memory for production provisioning')
 sys.path.insert(0,str(REPO/'src'));from h7607a.hardware import PACKAGES
 manifest=json.loads((REPO/'build/repo/repository.json').read_text())
 state['phase']='PROVISIONING';save(WORK/'state.json',state);create_esp(root,state)
 # The build VM cannot autodetect physical P16 storage or input controllers.
 write_text(root,'etc/mkinitcpio.conf','MODULES=()\nBINARIES=()\nFILES=()\nHOOKS=(base udev modconf keyboard block filesystems fsck)\nCOMPRESSION=zstd\nMODULES_DECOMPRESS=no\n')
 # Only the nine device hardware/firmware/power packages enter the live OS.
 wanted=set(PACKAGES)|{'power-profiles-daemon-n1x'}
 available={row['name']:row for row in manifest['packages']}
 records=[available[name]for name in sorted(wanted)]
 localkey=json.loads((REPO/'locks/packages.json').read_text())['signing_keys']['local']
 chroot(root,['pacman-key','--add','/source/'+localkey['path']])
 chroot(root,['pacman-key','--lsign-key',localkey['fingerprint']])
 chroot(root,['pacman','-U','--noconfirm',*['/source/build/repo/'+r['file']['path']for r in records]])
 finish_provision()

def finish_provision():
 state=checked_work();root=mounted_root(state);sys.path.insert(0,str(REPO/'src'))
 from h7607a.hardware import install_hardware
 hardware=install_hardware(root,repo_root=REPO)
 hygiene(root)
 run(['fstrim','-v',root])
 save(WORK/'provision.json',{'schema':'h7607a-live-provision-v1','hardware':hardware,'package_repository_sha256':digest(REPO/'build/repo/repository.json')})
 state['phase']='PROVISIONED';state['live_tools_lock_sha256']=digest(REPO/'locks/live-tools.json');save(WORK/'state.json',state)
 print('Configured live filesystem and module dependency indices.',flush=True)


def embed():
 state=checked_work();root=mounted_root(state);sys.path.insert(0,str(REPO/'src'))
 from h7607a.release import snapshot
 from h7607a.hardware import write_boot_files
 from h7607a.policy import configure_system_policy
 target=root/'opt/h7607a-arch';remove_path(root,'opt/h7607a-arch');target.parent.mkdir(exist_ok=True)
 snapshot(REPO,target);(target/'build').mkdir();shutil.copytree(REPO/'build/repo',target/'build/repo',symlinks=True)
 state['live_system_policy']=configure_system_policy(root,repo_root=REPO)
 write_text(root,'etc/machine-id','')
 chroot(root,['mkinitcpio','-p','linux-n1x-nvidia-bin'])
 boot=write_boot_files(root,root/'boot',['root=UUID='+lock()['root_uuid']],root_uuid=lock()['root_uuid'],repo_root=REPO,installer=True)
 chroot(root,['bootctl','--esp-path=/boot','--no-variables','install'])
 remove_path(root,'boot/loader/random-seed')
 provision=json.loads((WORK/'provision.json').read_text());provision['boot']=boot;save(WORK/'provision.json',provision)
 write_text(root,'usr/local/bin/archinstall','#!/bin/sh\nexec /usr/bin/python3 -B /opt/h7607a-arch/installer/h7607a.py "$@"\n',0o755)
 chroot(root,['python3','-B','-c',"import sys;sys.path.insert(0,'/opt/h7607a-arch/src');from h7607a.packages import configure_live;configure_live('/opt/h7607a-arch/build/repo',source_root='/opt/h7607a-arch')"])
 state['release_manifest_sha256']=digest(REPO/'release.json');state['phase']='EMBEDDED';save(WORK/'state.json',state)
 print('Embedded installer source, package repository, initramfs and EFI loader.',flush=True)


def seal():
 state=checked_work();root=mounted_root(state)
 for directory in ('var/log','var/tmp','tmp'):
  for path in (root/directory).iterdir():remove_path(root,str(path.relative_to(root)))
 remove_path(root,'var/lib/systemd/random-seed');write_text(root,'etc/machine-id','')
 # Stop only the chroot's keyring agent before releasing its filesystems.
 subprocess.run(['arch-chroot',str(root),'gpgconf','--homedir','/etc/pacman.d/gnupg','--kill','all'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 if mount_info(root/'source'):run(['umount',root/'source'])
 mounts=run(['findmnt','--json','-R',root,'-o','TARGET'],capture_output=True,text=True).stdout
 tree=json.loads(mounts)['filesystems'];children=tree[0].get('children',[])
 need({x['target']for x in children}=={str(root/'boot')}and all(not x.get('children')for x in children),'Unexpected remaining chroot submount')
 run(['fstrim','-v',root]);run(['sync','-f',root]);run(['umount',root/'boot']);run(['umount',root])
 run(['losetup','-d',state['esp_loop'],state['loop']]);run(['udevadm','settle','--timeout=10'])
 images={}
 for role in ('ESP','root'):
  path=WORK/(role+'.img');path.chmod(0o400);images[role]={'bytes':path.stat().st_size,'sha256':digest(path)}
 manifest={'schema':'h7607a-clean-live-build-v1','sealed':True,'root_uuid':lock()['root_uuid'],'source_lock_sha256':digest(REPO/'locks/live-base.json'),'source_sha256':lock()['source_sha256'],'release_manifest_sha256':state['release_manifest_sha256'],'package_repository_sha256':digest(REPO/'build/repo/repository.json'),'images':images,'provision':json.loads((WORK/'provision.json').read_text())}
 save(WORK/'build-manifest.json',manifest);state['phase']='SEALED';state['manifest_sha256']=digest(WORK/'build-manifest.json');save(WORK/'state.json',state)
 print(json.dumps(manifest,indent=2),flush=True)


def export_image(role,expected_manifest_sha):
 state=checked_work();need(state['phase']=='SEALED','Only sealed images can be exported')
 manifest=WORK/'build-manifest.json';need(re.fullmatch('[0-9a-f]{64}',expected_manifest_sha or'')and digest(manifest)==state['manifest_sha256']==expected_manifest_sha,'Sealed manifest differs from --manifest-sha256')
 row=json.loads(manifest.read_text())['images'][role];path=WORK/(role+'.img')
 need(not mount_info(WORK/'root')and not mount_info(WORK/'root/boot'),'Build images remain mounted')
 need(path.is_file()and not path.is_symlink()and path.stat().st_size==row['bytes']and path.stat().st_mode&0o222==0,'Sealed image identity/mode differs')
 with path.open('rb')as source:
  before=os.fstat(source.fileno());h=hashlib.sha256();count=0
  while block:=source.read(CHUNK):sys.stdout.buffer.write(block);h.update(block);count+=len(block)
  after=os.fstat(source.fileno())
 need((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)and count==row['bytes']and h.hexdigest()==row['sha256'],'Image changed during export')
 sys.stdout.buffer.flush()


def build():
 """Construct the live root and ESP from pinned packages and source files."""
 need(not WORK.exists(),'Build workspace already exists; remove or select a fresh workspace')
 for phase in (prepare,live_tools,provision,embed,seal):phase()


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['build','prepare','live-tools','provision','finish-provision','embed','seal','export','status']);p.add_argument('--image',choices=['ESP','root']);p.add_argument('--manifest-sha256');args=p.parse_args()
 need(sys.platform=='linux' and os.geteuid()==0 and platform.machine()=='aarch64','Run inside native ARM64 Linux VM as root')
 if args.phase=='export':need(args.image,'Image role required');export_image(args.image,args.manifest_sha256)
 elif args.phase=='status':print(json.dumps(checked_work(),indent=2))
 else:globals()[args.phase.replace('-','_')]()
if __name__=='__main__':
 try:main()
 except Exception as e:print('Live build stopped: '+str(e),file=sys.stderr);raise SystemExit(1)
