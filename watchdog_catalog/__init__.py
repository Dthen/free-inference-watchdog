"""Standalone public catalogue package.

Inert package root: importing this package performs no runtime configuration
loading, reads no files, resolves no credentials and performs no I/O. It
intentionally re-exports nothing and defines no runtime version or resource;
the pure raw-JSON boundary and the typed canonical encoder live in
:mod:`watchdog_catalog.catalog`, and later units add their own modules here.
"""
