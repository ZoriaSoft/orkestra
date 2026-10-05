"""orkestra — LLM provider/model registry and layered agent orchestration.

Phase 1 provides the provider + model registries and the ``orkestra`` CLI.
The orchestration engine (sef -> hamal -> kalfa -> birlestirici) builds on
:meth:`orkestra.registry.Registry.resolve` in a later phase.
"""

__version__ = "0.1.0"
