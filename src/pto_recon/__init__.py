"""BambooHR <-> Harvest PTO reconciliation.

The write path (config -> bamboo/harvest -> reconcile -> ledger -> runner) is
deterministic arithmetic end to end. No module in this package imports or
calls a language model. The advisory tooling lives in the separate
``pto_advisory`` package, which has no access to the BambooHR write client.
"""

__version__ = "1.0.0"
