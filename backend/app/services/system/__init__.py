"""Cross-domain platform maintenance.

Services here belong to no single feature domain: they reconcile state that
several domains write. The first tenant is ``status_repair_scheduler``, which
repairs rows abandoned in a transitional status (environments, sessions, input
tasks, channel deliveries).

Nothing is re-exported — import the submodule directly. A domain service must
never import from here; the dependency runs one way, from the reconciler down
into each domain's own service layer.
"""
