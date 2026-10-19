# Literal substitutions that acpi_builder.py applies to the ACPICA iasl
# 20260408 disassembly (iasl -d SSDT0001.bin) of the H7607BA.300 ASUS SSDT;
# sed -f with this file gives the same power-source.dsl.
# DefinitionBlock: OEM revision 2
21s/0x00000001)/0x00000002)/
# \_SB.EC0.RRA9
3590,3604s/Local0/RSPB/g
3590s/RSPB = Buffer/Name (RSPB, Buffer/
3590s/{}/{})/
# \_SB.EC0.RRA8
3623,3637s/Local0/RSPB/g
3623s/RSPB = Buffer/Name (RSPB, Buffer/
3623s/{}/{})/
# \_SB.EC0.WRA9
3656,3671s/Local0/RSPB/g
3656s/RSPB = Buffer/Name (RSPB, Buffer/
3656s/{}/{})/
# \_SB.EC0.WRA8
3689,3704s/Local0/RSPB/g
3689s/RSPB = Buffer/Name (RSPB, Buffer/
3689s/{}/{})/
# \_SB.EC0.RWEC
3728,3765s/Local0/RSPB/g
3728s/RSPB = Buffer/Name (RSPB, Buffer/
3728s/{}/{})/
# \_SB.BAT0.GBST
5410,5423s/Local5/RSPB/g
5410s/RSPB = Buffer/Name (RSPB, Buffer/
5410s/{}/{})/
