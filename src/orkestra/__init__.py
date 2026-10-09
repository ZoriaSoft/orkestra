"""orkestra — LLM provider/model registry and layered agent orchestration.

Phase 1 provides the provider + model registries and the ``orkestra`` CLI.
Phase 2 adds the orchestration engine: ``orkestra run`` conducts a run of
sef (decompose) -> hamal pool (cheap workers) -> kalfa (validate) ->
birlestirici (synthesize), with per-call usage logging and budget valves.
"""

__version__ = "0.2.1"
