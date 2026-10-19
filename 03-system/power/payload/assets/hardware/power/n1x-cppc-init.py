"""Restore CPPC range and enable firmware autonomous selection on NVIDIA N1X.

Uses the sysfs ABI in NVIDIA/NV-Kernels drivers/cpufreq/cppc_cpufreq.c.
Run as root. Has no effect without the NVIDIA MT8901 ACPI device.
"""
from pathlib import Path

if any(Path('/sys/bus/acpi/devices').glob('NVDA0200:*')):
    for policy in Path('/sys/devices/system/cpu/cpufreq').glob('policy*'):
        driver = policy / 'scaling_driver'
        auto = policy / 'auto_select'
        if not driver.exists() or driver.read_text().strip() != 'cppc_cpufreq' or not auto.exists():
            continue
        low = int((policy / 'cpuinfo_min_freq').read_text())
        high = int((policy / 'cpuinfo_max_freq').read_text())
        if not 0 < low <= high:
            raise RuntimeError(f'Invalid frequency range in {policy}')
        auto.write_text('0\n')
        (policy / 'scaling_min_freq').write_text(f'{low}\n')
        (policy / 'scaling_max_freq').write_text(f'{high}\n')
        auto.write_text('1\n')
        print(f'{policy.name}: {low}..{high} kHz; autonomous selection enabled')
