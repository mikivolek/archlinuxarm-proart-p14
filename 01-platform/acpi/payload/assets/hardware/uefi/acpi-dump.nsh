@echo -off
# Dump this P16's firmware DSDT and SSDTs to \H7607A-ACPI on the stick that
# holds this Shell, as input for provenance/hardware/power-archive-builder.py.
if x%homefilesystem% == x then
  echo STOP: this Shell has no own-filesystem mapping.
  exit /b 1
endif
%homefilesystem%
if not %lasterror% == 0 then
  echo STOP: cannot select the dump stick.
  exit /b 1
endif
if not exist \H7607A-ACPI then
  mkdir \H7607A-ACPI
endif
cd \H7607A-ACPI
if not %lasterror% == 0 then
  echo STOP: cannot enter \H7607A-ACPI on the dump stick.
  exit /b 1
endif
# acpiview writes into an existing dump file without truncating it.
if exist *.bin then
  rm -q *.bin
endif
acpiview -s DSDT -d
acpiview -s SSDT -d
if not exist DSDT0000.bin then
  echo STOP: no DSDT was dumped. This Shell needs the acpiview command.
  exit /b 1
endif
if not exist SSDT0000.bin then
  echo STOP: no SSDT was dumped.
  exit /b 1
endif
echo Dumped to %homefilesystem%\H7607A-ACPI. Power off, then pass that folder
echo to power-archive-builder.py on the build machine.
