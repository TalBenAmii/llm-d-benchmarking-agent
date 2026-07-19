"""Pydantic input models for every agent tool. These are the single source of truth
for each tool's argument schema: they validate the LLM's tool-call arguments
(determinism gate a) and are exported as JSON Schema in the provider tool definitions.

This package re-exports every public input model so existing call sites can keep importing
`from app.tools.schemas import X` unchanged; the models live in per-tool-group submodules
(probe, docs, command, config, execute, orchestrate, analysis, doe, provenance).
"""
from __future__ import annotations

from app.tools.schemas.analysis import (
    AggregateRunsInput,
    AnalyzeResultsInput,
    CheckCapacityInput,
    CompareHarnessRunsInput,
    CompareReportsInput,
)
from app.tools.schemas.command import (
    EnsureReposInput,
    LocateReportInput,
    RunSetupInput,
    RunShellInput,
)
from app.tools.schemas.config import (
    ConvertGuideInput,
    WriteConfigInput,
)
from app.tools.schemas.docs import (
    FetchKeyDocsInput,
    ReadKnowledgeInput,
    ReadRepoDocInput,
    SearchKnowledgeInput,
)
from app.tools.schemas.doe import (
    DoEFactor,
    GenerateDoeInput,
)
from app.tools.schemas.execute import ExecuteInput
from app.tools.schemas.orchestrate import (
    CancelRunInput,
    ManageOrchestratedRunsInput,
    OrchestrateBenchmarkInput,
    OrchestrateSweepInput,
    SweepTreatment,
)
from app.tools.schemas.probe import (
    AdviseAcceleratorsInput,
    CheckEndpointReadinessInput,
    DiscoverStackInput,
    ListCatalogInput,
    ProbeEnvironmentInput,
)
from app.tools.schemas.provenance import (
    EstimateRunDurationInput,
    ExportRunBundleInput,
    InspectWorkloadProfileInput,
    NextStepSuggestion,
    ObserveRunMetricsInput,
    ProvisionHfSecretInput,
    ReproduceRunInput,
    ResultHistoryInput,
    SuggestNextStepsInput,
)

__all__ = [
    "AdviseAcceleratorsInput",
    "AggregateRunsInput",
    "AnalyzeResultsInput",
    "CancelRunInput",
    "CheckCapacityInput",
    "CheckEndpointReadinessInput",
    "CompareHarnessRunsInput",
    "CompareReportsInput",
    "ConvertGuideInput",
    "DiscoverStackInput",
    "DoEFactor",
    "EnsureReposInput",
    "EstimateRunDurationInput",
    "ExecuteInput",
    "ExportRunBundleInput",
    "FetchKeyDocsInput",
    "GenerateDoeInput",
    "InspectWorkloadProfileInput",
    "ListCatalogInput",
    "LocateReportInput",
    "ManageOrchestratedRunsInput",
    "NextStepSuggestion",
    "ObserveRunMetricsInput",
    "OrchestrateBenchmarkInput",
    "OrchestrateSweepInput",
    "ProbeEnvironmentInput",
    "ProvisionHfSecretInput",
    "ReadKnowledgeInput",
    "ReadRepoDocInput",
    "ReproduceRunInput",
    "ResultHistoryInput",
    "RunSetupInput",
    "RunShellInput",
    "SearchKnowledgeInput",
    "SuggestNextStepsInput",
    "SweepTreatment",
    "WriteConfigInput",
]
