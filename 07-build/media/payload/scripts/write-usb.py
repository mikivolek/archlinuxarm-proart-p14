#!/usr/bin/env python3
"""Replace only this identified USB's ESP/root from a sealed clean VM build.

No partition table, raw carrier, or Windows backup partition can be written.
Default mode is read-only preflight. --write additionally requires the exact
reviewed build-manifest SHA and the explicit device confirmation name.
"""
import argparse,datetime,fcntl,hashlib,json,os,plistlib,re,selectors,signal,stat,struct,subprocess,sys,time,uuid
from pathlib import Path
sys.dont_write_bytecode=True
REPO=Path(__file__).resolve().parents[1]
ESP=(1048576,805306368);ROOT=(806354944,9930014720);WHOLE=61530439680
BLOCK_SIZE_IOCTL=0x40046418;BLOCK_COUNT_IOCTL=0x40086419;SYNC_IOCTL=0x20006416
CHUNK=1024*1024

def need(v,m):
 if not v:raise RuntimeError(m)
def digest(p):
 with Path(p).open('rb')as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(path,value):
 with path.open('x')as f:json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
 os.chmod(path,0o600)
def disk(*args):
 p=subprocess.run(['/usr/sbin/diskutil',*args],stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=30)
 need(not p.stderr.strip(),'diskutil emitted unexpected diagnostics');return plistlib.loads(p.stdout)
def load_layout():
 p=REPO/'locks/usb-layout.json';need(p.is_file()and not p.is_symlink(),'USB layout lock absent')
 value=json.loads(p.read_text());need(value['schema']=='h7607a-existing-analysis-usb-layout-v1'and value['whole_bytes']==WHOLE,'Unexpected layout contract')
 need([(x['offset'],x['bytes'])for x in value['partitions']]==[ESP,ROOT,(21474836480,40055586304)],'Partition range contract differs')
 return value

def identify(layout):
 found=[]
 for row in disk('list','-plist','external','physical').get('AllDisksAndPartitions',[]):
  name=row.get('DeviceIdentifier','')
  if not re.fullmatch('disk[0-9]+',name):continue
  whole=disk('info','-plist','/dev/'+name)
  if (whole.get('Internal')is False and whole.get('WholeDisk')is True and whole.get('BusProtocol')=='USB'and whole.get('VirtualOrPhysical')=='Physical'and whole.get('TotalSize')==WHOLE and whole.get('MediaName')=='Ultra USB 3.0'):found.append((name,row))
 need(len(found)==1,'Expected one exact external physical SanDisk USB')
 name,row=found[0];parts=row.get('Partitions',[])
 need(len(parts)==3 and {x['DeviceIdentifier']for x in parts}=={name+'s'+str(n)for n in(1,2,3)},'Exact three partitions required')
 for part in layout['partitions']:
  node=name+'s'+str(part['number']);info=disk('info','-plist','/dev/'+node)
  need(info.get('DeviceIdentifier')==node and info.get('ParentWholeDisk')==name and info.get('Internal')is False and info.get('WholeDisk')is False and info.get('IOKitSize')==part['bytes']and info.get('PartitionMapPartitionOffset')==part['offset'],'Partition identity differs')
  if part['number']==1:need(info.get('VolumeName')=='N1XESP','ESP label differs')
  if part['number']==2:need(not info.get('MountPoint'),'USB Linux root must remain unmounted')
  if part['number']==3:need(info.get('FilesystemType')=='exfat'and info.get('VolumeName')=='P16BACKUP'and str(info.get('DiskUUID','')).lower()==part['partuuid'],'Backup identity differs')
 return name

def no_mounts(name):
 p=subprocess.run(['/sbin/mount'],capture_output=True,text=True,check=True,timeout=15)
 need(not re.search(r'(?<![A-Za-z0-9])/dev/r?'+re.escape(name)+r'(?:s[0-9]+)?(?:\s|$)',p.stdout),'USB remains in kernel mount table')

def raw_geometry(fd,name,rdev):
 current=os.fstat(fd);node=os.stat('/dev/r'+name,follow_symlinks=False)
 need(stat.S_ISCHR(current.st_mode)and stat.S_ISCHR(node.st_mode)and current.st_rdev==node.st_rdev==rdev,'Raw descriptor identity changed')
 block,count=bytearray(4),bytearray(8);fcntl.ioctl(fd,BLOCK_SIZE_IOCTL,block,True);fcntl.ioctl(fd,BLOCK_COUNT_IOCTL,count,True)
 need(struct.unpack('<I',block)[0]==512 and struct.unpack('<Q',count)[0]*512==WHOLE,'Raw capacity/sector size differs')

def read_exact(fd,n,offset):
 # macOS raw character devices require whole-sector offsets and lengths.
 # Hash only the requested object bytes, with a bounded sector bounce buffer.
 need(type(n)is int and type(offset)is int and n>0 and 0<=offset<offset+n<=WHOLE,'Raw read outside device bounds')
 start=offset//512*512;end=((offset+n+511)//512)*512
 need(end<=WHOLE,'Aligned raw read exceeds device')
 data=bytearray();total=end-start
 while len(data)<total:
  part=os.pread(fd,total-len(data),start+len(data))
  need(part and len(part)%512==0 and len(part)<=total-len(data),'Unexpected short or unaligned raw read')
  data.extend(part)
 return bytes(data[offset-start:offset-start+n])

def hash_region(fd,offset,size):
 h=hashlib.sha256();done=0
 while done<size:
  piece=read_exact(fd,min(CHUNK,size-done),offset+done);h.update(piece);done+=len(piece)
 return h.hexdigest()

def protected_proof(fd,layout,*,full_carrier=True):
 metadata={'mbr':(0,512),'primary':(512,512),'primary_array':(1024,16384),'backup_array':(WHOLE-33*512,16384),'backup':(WHOLE-512,512)}
 gpt={k:hash_region(fd,*v)for k,v in metadata.items()};need(gpt==layout['gpt_sha256'],'GPT/partition table differs from locks/usb-layout.json')
 raw=read_exact(fd,512,layout['backup_boot_identity']['offset']);b=layout['backup_boot_identity']
 need(raw[3:11]==b'EXFAT   'and raw[510:]==b'\x55\xaa'and raw[108]==b['sector_shift']and raw[109]==b['cluster_shift']and struct.unpack_from('<Q',raw,64)[0]*512==b['offset']and struct.unpack_from('<Q',raw,72)[0]*512==b['bytes']and struct.unpack_from('<I',raw,100)[0]==b['serial'],'Backup exFAT boot identity differs')
 # Both main and backup exFAT boot regions are independently preserved.
 backup_boot=hash_region(fd,b['offset'],24*512)
 header=hash_region(fd,layout['carrier_header_offset'],layout['carrier_header_bytes']);need(header==layout['carrier_header_sha256'],'Raw carrier header differs')
 count=0
 if full_carrier:
  for row in layout['carrier_objects']:
   need(17180917760<=row['offset']<row['offset']+row['bytes']<=21474836480,'Carrier range overlaps writable/backup space')
   need(hash_region(fd,row['offset'],row['bytes'])==row['sha256'],'Frozen carrier object differs');count+=1
 return {'gpt_sha256':gpt,'backup_boot_regions_sha256':backup_boot,'carrier_header_sha256':header,'carrier_objects_verified':count}

def validate_build(path,expected_sha):
 need(path.is_file()and not path.is_symlink()and path.parent.resolve()==path.parent and re.fullmatch('[0-9a-f]{64}',expected_sha or''),'Pass an existing --build-manifest and its --build-sha256')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
 try:
  before=os.fstat(fd);need(stat.S_ISREG(before.st_mode)and 0<before.st_size<=2*1024**2,'Bounded regular build manifest required')
  data=os.read(fd,before.st_size+1);after=os.fstat(fd);current=path.stat(follow_symlinks=False)
  need(len(data)==before.st_size and (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)and (current.st_dev,current.st_ino)==(before.st_dev,before.st_ino)and hashlib.sha256(data).hexdigest()==expected_sha,'Build manifest changed while reading or differs from --build-sha256')
 finally:os.close(fd)
 b=json.loads(data);need(b.get('schema')=='h7607a-clean-live-build-v1'and b.get('sealed')is True,'Build is not sealed')
 need(set(b.get('images',{}))=={'ESP','root'},'Build image set differs')
 for role,window in [('ESP',ESP),('root',ROOT)]:
  row=b['images'][role];need(row.get('bytes')==window[1]and re.fullmatch('[0-9a-f]{64}',row.get('sha256','')),'Build image size/hash missing')
 need(b.get('source_lock_sha256')==digest(REPO/'locks/live-base.json'),'Build source lock differs')
 need(b.get('root_uuid')==json.loads((REPO/'locks/live-base.json').read_text())['root_uuid'],'Live root UUID differs')
 return b

def allowed_write(offset,size,window):
 need(window in(ESP,ROOT)and size>0 and offset%512==0 and size%512==0 and window[0]<=offset<offset+size<=sum(window),'Write outside exact ESP/root partition bounds')

def write_chunk(fd,data,offset,window):
 allowed_write(offset,len(data),window);view=memoryview(data)
 while view:
  n=os.pwrite(fd,view,offset);need(n>0 and n%512==0,'Short/unaligned raw write');offset+=n;view=view[n:]

def stream_image(fd,window,role,row,log,uid,manifest_sha=None):
 command=['/usr/bin/sudo','-u','#'+str(uid),'/opt/homebrew/bin/limactl','shell','--workdir=/tmp','h7607a-build','--','sudo','python3','-B','/mnt/h7607a-source/scripts/build-live.py','export','--image',role,'--manifest-sha256',manifest_sha or'']
 with log.open('xb')as err:
  proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=err,start_new_session=True)
  selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ);deadline=time.monotonic()+1800;count=0;h=hashlib.sha256();pending=bytearray()
  try:
   while True:
    need(time.monotonic()<deadline,'VM image export timed out')
    if not selector.select(min(5,max(0,deadline-time.monotonic()))):continue
    data=os.read(proc.stdout.fileno(),CHUNK)
    if not data:break
    need(count+len(pending)+len(data)<=window[1],'Exported image exceeds pinned partition size');pending.extend(data)
    while len(pending)>=CHUNK:
     piece=bytes(pending[:CHUNK]);del pending[:CHUNK];write_chunk(fd,piece,window[0]+count,window);h.update(piece);count+=len(piece)
   if pending:piece=bytes(pending);write_chunk(fd,piece,window[0]+count,window);h.update(piece);count+=len(piece)
   need(proc.wait(timeout=30)==0 and count==row['bytes']and h.hexdigest()==row['sha256'],'VM exported image hash/length/exit differs')
   return {'bytes':count,'sha256':h.hexdigest()}
  finally:
   selector.close();proc.stdout.close()
   if proc.poll()is None:
    os.killpg(proc.pid,signal.SIGTERM)
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--write',action='store_true');p.add_argument('--device');p.add_argument('--build-manifest',type=Path);p.add_argument('--build-sha256');a=p.parse_args()
 need(sys.platform=='darwin'and os.geteuid()==0,'Run on Mac in retained administrator terminal')
 owner=REPO.stat();need(owner.st_uid!=0,'Expected user-owned repository');layout=load_layout();name=identify(layout)
 if a.write:need(a.device=='/dev/'+name,'--device must name the identified USB')
 build=validate_build(a.build_manifest,a.build_sha256)if a.write else None
 # Unmount only the identified USB before any raw descriptor opens.
 for n in(1,3):
  part='/dev/'+name+'s'+str(n)
  if disk('info','-plist',part).get('MountPoint'):subprocess.run(['/usr/sbin/diskutil','unmount',part],check=True,timeout=30)
 need(identify(layout)==name,'USB identity changed during unmount');no_mounts(name)
 out=REPO/'.work/media';out.mkdir(parents=True,exist_ok=True);stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
 receipt=out/('usb-'+stamp+'.json');report={'schema':'h7607a-live-usb-write-v1','success':False,'device':'/dev/'+name,'write_requested':a.write,'write_started':False,'GPT_written':False,'carrier_written':False,'backup_partition_written':False,'backup_full_content_rehashed':False,'allowed_write_ranges':[ESP,ROOT],'writer_sha256':digest(Path(__file__)),'layout_lock_sha256':digest(REPO/'locks/usb-layout.json')};fd=None
 try:
  fd=os.open('/dev/r'+name,(os.O_RDWR if a.write else os.O_RDONLY)|os.O_NOFOLLOW|os.O_CLOEXEC);rdev=os.fstat(fd).st_rdev;raw_geometry(fd,name,rdev);report['protected_before']=protected_proof(fd,layout)
  if a.write:
   report['build_manifest_sha256']=a.build_sha256;report['written']={}
   for role,window in [('root',ROOT),('ESP',ESP)]:
    no_mounts(name);raw_geometry(fd,name,rdev);report['write_started']=True;print('Writing clean '+role+' partition; Windows backup and raw cache remain protected.',flush=True)
    report['written'][role]=stream_image(fd,window,role,build['images'][role],out/('export-'+role+'-'+stamp+'.log'),owner.st_uid,a.build_sha256)
   os.fsync(fd);fcntl.ioctl(fd,SYNC_IOCTL,0);report['hardware_cache_barrier_completed']=True
   report['readback']={}
   for role,window in [('root',ROOT),('ESP',ESP)]:
    actual=hash_region(fd,*window);need(actual==build['images'][role]['sha256'],'USB full partition readback differs: '+role);report['readback'][role]={'bytes':window[1],'sha256':actual}
   report['protected_after']=protected_proof(fd,layout);need(report['protected_after']==report['protected_before'],'Protected regions changed')
  os.close(fd);fd=None
  if a.write:subprocess.run(['/usr/sbin/diskutil','eject','/dev/'+name],check=True,timeout=30);report['safely_ejected']=True
  report['success']=True;return 0
 except Exception as e:report['error']=type(e).__name__+': '+str(e);raise
 finally:
  if fd is not None:os.close(fd)
  save(receipt,report);os.chown(receipt,owner.st_uid,owner.st_gid);print('Media receipt: '+str(receipt),flush=True)
if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as e:print('Media operation stopped: '+str(e),file=sys.stderr);raise SystemExit(1)
