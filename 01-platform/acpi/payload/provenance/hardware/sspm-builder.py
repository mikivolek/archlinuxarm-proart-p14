#!/usr/bin/env python3
"""Check the exact H7607BA SSPM override against this P16's own DSDT dump."""
from acpi_builder import cli

if __name__ == "__main__":
    cli(sspm_only=True)
