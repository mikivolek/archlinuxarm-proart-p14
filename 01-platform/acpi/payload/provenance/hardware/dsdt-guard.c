/* SPDX-License-Identifier: MIT
 * Read-only input-file guard for the exact P16 DSDT used by the RAM override.
 * All protocol definitions are unmodified official EDK2 headers (see provenance).
 * Opens inputs only on this application's LoadedImage.DeviceHandle filesystem.
 * Diagnostic text goes only to the caller-provided Shell StdOut stream.
 */
#include <Uefi.h>
#include <Protocol/LoadedImage.h>
#include <Protocol/SimpleFileSystem.h>
#include <Protocol/ShellParameters.h>
#include <Guid/FileInfo.h>
#include "guard-core.h"

_Static_assert(sizeof(UINTN)==8 && sizeof(EFI_STATUS)==8 && sizeof(CHAR16)==2,"ARM64 UEFI integer ABI");
_Static_assert(OFFSET_OF(EFI_SYSTEM_TABLE,ConOut)==64 && OFFSET_OF(EFI_SYSTEM_TABLE,BootServices)==96,"system table ABI");
_Static_assert(OFFSET_OF(EFI_BOOT_SERVICES,HandleProtocol)==152,"boot services ABI");
_Static_assert(OFFSET_OF(EFI_LOADED_IMAGE_PROTOCOL,DeviceHandle)==24,"loaded image ABI");
_Static_assert(OFFSET_OF(EFI_SIMPLE_FILE_SYSTEM_PROTOCOL,OpenVolume)==8,"filesystem ABI");
_Static_assert(OFFSET_OF(EFI_FILE_PROTOCOL,Read)==32 && OFFSET_OF(EFI_FILE_PROTOCOL,GetInfo)==64,"file ABI");
_Static_assert(OFFSET_OF(EFI_SHELL_PARAMETERS_PROTOCOL,Argc)==8,"shell parameters ABI");

static EFI_GUID loaded_image_guid=EFI_LOADED_IMAGE_PROTOCOL_GUID;
static EFI_GUID filesystem_guid=EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID;
static EFI_GUID shell_parameters_guid=EFI_SHELL_PARAMETERS_PROTOCOL_GUID;
static EFI_GUID shell_protocol_guid=EFI_SHELL_PROTOCOL_GUID;
static EFI_GUID file_info_guid=EFI_FILE_INFO_ID;
static UINT8 baseline[N1X_DSDT_SIZE], current[N1X_DSDT_SIZE];
static EFI_SHELL_PARAMETERS_PROTOCOL *shell;
static EFI_SHELL_PROTOCOL *shell_protocol;
static BOOLEAN output_failed=FALSE;

static BOOLEAN same_string(CONST CHAR16 *a,CONST CHAR16 *b){
 UINTN n=0;for(;n<160;n++){if(a[n]!=b[n])return FALSE;if(!a[n])return TRUE;}return FALSE;
}
static BOOLEAN valid_current_path(CONST CHAR16 *p){
 CONST CHAR16 *prefix=L"\\N1X-EFI-DIAG-V2-";UINTN n=0,digits=0;
 while(prefix[n]){if(p[n]!=prefix[n])return FALSE;n++;}
 while(p[n]>='0'&&p[n]<='9'&&digits<3){digits++;n++;}
 if(digits<1||digits>2)return FALSE;
 return same_string(p+n,L"\\DSDT0000.bin");
}
static VOID message(CONST CHAR16 *text){
 UINTN count=0,actual;EFI_STATUS status;
 if(!shell||!shell->StdOut||!shell_protocol||!shell_protocol->WriteFile){output_failed=TRUE;return;}
 while(text[count])count++;
 count*=sizeof(CHAR16);actual=count;
 status=shell_protocol->WriteFile(shell->StdOut,&actual,(VOID*)text);
 if(status!=EFI_SUCCESS||actual!=count)output_failed=TRUE;
}
static EFI_STATUS read_exact(EFI_FILE_PROTOCOL *root,CONST CHAR16 *name,UINT8 *buffer){
 EFI_FILE_PROTOCOL *file=NULL;EFI_STATUS status,close_status;
 union {UINT64 alignment;UINT8 bytes[512];} info_buffer;
 EFI_FILE_INFO *info=(EFI_FILE_INFO*)info_buffer.bytes;
 UINTN info_size=sizeof(info_buffer.bytes),offset=0,wanted,actual;
 UINT8 extra;
 status=root->Open(root,&file,(CHAR16*)name,EFI_FILE_MODE_READ,0);
 if(status!=EFI_SUCCESS||!file)return status==EFI_SUCCESS?EFI_DEVICE_ERROR:status;
 if(!file->GetInfo||!file->Read||!file->Close){status=EFI_UNSUPPORTED;goto done;}
 status=file->GetInfo(file,&file_info_guid,&info_size,info);
 if(status!=EFI_SUCCESS)goto done;
 if(info_size<82||info_size>sizeof(info_buffer.bytes)||info->Size!=info_size||
    info->FileSize!=N1X_DSDT_SIZE||(info->Attribute&EFI_FILE_DIRECTORY)){
  status=EFI_BAD_BUFFER_SIZE;goto done;
 }
 while(offset<N1X_DSDT_SIZE){
  wanted=N1X_DSDT_SIZE-offset;if(wanted>4096)wanted=4096;
  actual=wanted;status=file->Read(file,&actual,buffer+offset);
  if(status!=EFI_SUCCESS)goto done;
  if(actual!=wanted){status=EFI_BAD_BUFFER_SIZE;goto done;}
  offset+=actual;
 }
 actual=1;status=file->Read(file,&actual,&extra);
 if(status==EFI_SUCCESS&&actual!=0)status=EFI_BAD_BUFFER_SIZE;
done:
 close_status=file->Close?file->Close(file):EFI_UNSUPPORTED;
 if(status==EFI_SUCCESS&&close_status!=EFI_SUCCESS)status=close_status;
 return status;
}

EFI_STATUS EFIAPI efi_main(EFI_HANDLE image,EFI_SYSTEM_TABLE *system){
 EFI_LOADED_IMAGE_PROTOCOL *loaded=NULL;
 EFI_SIMPLE_FILE_SYSTEM_PROTOCOL *filesystem=NULL;
 EFI_FILE_PROTOCOL *root=NULL;EFI_STATUS status,close_status;
 if(!system||!system->BootServices||!system->BootServices->HandleProtocol)return EFI_UNSUPPORTED;
 status=system->BootServices->HandleProtocol(image,&shell_parameters_guid,(VOID**)&shell);
 if(status!=EFI_SUCCESS||!shell||shell->Argc!=3||!shell->Argv||!shell->Argv[1]||!shell->Argv[2])return EFI_INVALID_PARAMETER;
 if(!system->BootServices->LocateProtocol)return EFI_UNSUPPORTED;
 status=system->BootServices->LocateProtocol(&shell_protocol_guid,NULL,(VOID**)&shell_protocol);
 if(status!=EFI_SUCCESS||!shell_protocol||!shell_protocol->WriteFile)return EFI_UNSUPPORTED;
 if(!valid_current_path(shell->Argv[1])||!same_string(shell->Argv[2],L"\\n1x-dsdt-baseline.bin")){
  message(L"FAIL: unexpected current-capture or baseline path.\r\n");return EFI_INVALID_PARAMETER;
 }
 status=system->BootServices->HandleProtocol(image,&loaded_image_guid,(VOID**)&loaded);
 if(status!=EFI_SUCCESS||!loaded||!loaded->DeviceHandle){message(L"FAIL: own loaded-image device is unavailable.\r\n");return EFI_NOT_FOUND;}
 status=system->BootServices->HandleProtocol(loaded->DeviceHandle,&filesystem_guid,(VOID**)&filesystem);
 if(status!=EFI_SUCCESS||!filesystem||!filesystem->OpenVolume){message(L"FAIL: own device has no filesystem.\r\n");return EFI_NOT_FOUND;}
 status=filesystem->OpenVolume(filesystem,&root);
 if(status!=EFI_SUCCESS||!root||!root->Open||!root->Close){message(L"FAIL: cannot open own filesystem root.\r\n");return EFI_DEVICE_ERROR;}
 status=read_exact(root,shell->Argv[2],baseline);
 if(status!=EFI_SUCCESS){message(L"FAIL: baseline size, read, EOF or close validation failed.\r\n");goto done;}
 if(!guard_baseline_valid(baseline,sizeof(baseline))){message(L"FAIL: baseline header, checksum or pinned SHA256 differs.\r\n");status=EFI_SECURITY_VIOLATION;goto done;}
 status=read_exact(root,shell->Argv[1],current);
 if(status!=EFI_SUCCESS){message(L"FAIL: current capture size, read, EOF or close validation failed.\r\n");goto done;}
 if(!guard_equal(current,sizeof(current),baseline,sizeof(baseline))){message(L"FAIL: current DSDT differs; RAM override must not boot.\r\n");status=EFI_SECURITY_VIOLATION;goto done;}
 status=EFI_SUCCESS;
done:
 close_status=root->Close(root);
 if(status==EFI_SUCCESS&&close_status!=EFI_SUCCESS)status=close_status;
 if(status==EFI_SUCCESS){
  message(L"PASS: complete current DSDT exactly matches the pinned 74052-byte baseline.\r\n");
  if(output_failed)status=EFI_DEVICE_ERROR;
 }
 /* Some Shell paths treat EFI warning statuses as a successful launch.
    Convert every nonzero outcome to a real EFI error before returning. */
 return status==EFI_SUCCESS?EFI_SUCCESS:EFI_ABORTED;
}
