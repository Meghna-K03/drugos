"""DrugOS Graph Module — Loader Protocol
=========================================
A ``Loader`` Protocol (PEP 544) that standardises the public surface of
every data-source loader in the package.

Why a Protocol?
  * ``run_pipeline.py`` currently special-cases every loader
    (``download_chembl`` vs ``download_uniprot`` vs ``download_drkg`` …).
    A Protocol lets the pipeline treat loaders polymorphically without a
    shared base class (composition over inheritance — D1-002).
  * ``@runtime_checkable`` means ``isinstance(obj, Loader)`` works at
    runtime, so the pipeline can assert a loader conforms before calling
    it.
  * A Protocol is *structural* — existing module-level functions do not
    need to inherit from anything. The ``UniProtLoader`` adapter class in
    ``uniprot_loader.py`` satisfies the Protocol by *duck typing*.

The Protocol is intentionally minimal (three methods). Each loader may
expose additional source-specific functions; only these three are
required for pipeline-level polymorphism.

Fixes: D1-002 (no Loader Protocol/ABC), D1-004 (__all__).

Loader Edge-Record Contract (v28 ROOT FIX P2-B-8)
---------------------------------------------------
Loaders emit edges as dicts. The kg_builder accepts the following
endpoint keys in priority order:

  * Source (head) endpoints: ``src_id`` (canonical), or one of the
    aliases ``drug_id``, ``source``, ``head``, ``from_id``, ``subject_id``.
  * Destination (tail) endpoints: ``dst_id`` (canonical), or one of the
    aliases ``target_uniprot_id``, ``target``, ``tail``, ``to_id``,
    ``object_id``.

IMPORTANT — "source" is BOTH an endpoint alias AND a legitimate
data-source PROPERTY:
  * When ``src_id`` is ABSENT, ``source`` (if present) is interpreted as
    the head endpoint ID, then REMOVED from the edge dict (it cannot
    also serve as a property).
  * When ``src_id`` is PRESENT, ``source`` (if present) is treated as a
    data-source PROPERTY (e.g. ``source="chembl"``) and preserved in
    the edge's props.

Contract for loader authors:
  * Loaders MUST emit either ``src_id`` / ``dst_id`` (preferred) OR one
    of the aliases — never both. Emitting both ``src_id`` and ``source``
    on the same edge is ambiguous and the alias resolution may strip
    the property.
  * Loaders that need to tag the data source SHOULD set the
    ``_source`` key (e.g. ``_source="chembl"``) instead of reusing
    ``source`` for that purpose, OR ensure ``src_id`` is always present
    (so ``source`` is unambiguously a property).
  * The current kg_builder (line ~1654) implements this contract
    correctly: it only removes the alias when it is ACTUALLY used as an
    endpoint (i.e. when ``src_id`` is absent and the alias was
    promoted). Loaders that follow this contract will never lose their
    data-source property.

This contract is enforced at kg_builder.load_edges_bulk_create. See
``kg_builder.py:1640-1683`` for the implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Protocol,
    Tuple,
    runtime_checkable,
)

__all__: list[str] = ["Loader"]


@runtime_checkable
class Loader(Protocol):
    """Structural contract for a DrugOS data-source loader.

    A loader has a ``name``, can ``download`` its raw file, ``parse`` it
    into an iterator of record dicts, and ``to_graph`` those records into
    a ``(nodes, edges)`` pair of lists.

    Implementations are NOT required to subclass this Protocol — any
    object with the three methods and a ``name`` attribute satisfies it
    (structural typing). The ``UniProtLoader`` adapter in
    ``uniprot_loader.py`` is the reference implementation.

    Methods
    -------
    download(force=False) -> Path
        Download (or cached-load) the raw source file.
    parse(path=None) -> Iterator[dict]
        Yield parsed records. Pure parser — no organism filter.
    to_graph(records) -> Tuple[List[dict], List[dict]]
        Convert records into ``(nodes, edges)`` for the KG.
    """

    name: str

    def download(self, force: bool = False) -> Path: ...

    def parse(self, path: Path | None = None) -> Iterator[Dict[str, Any]]: ...

    def to_graph(
        self, records: Any
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]: ...
