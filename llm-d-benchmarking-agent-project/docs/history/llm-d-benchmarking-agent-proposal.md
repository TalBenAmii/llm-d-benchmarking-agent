**PROJECT PROPOSAL** Parallel and Distributed Project Lab – Technion 

## **LLM Inference Benchmarking Agent** 

_An Intelligent, Kubernetes-Native Workload Design and Execution Service for the llm-d Distributed Inference Platform_ 

**Proposed by** Maroon Ayoub, IBM Research **Affiliation** llm-d Open-Source Project **Date** February 2026 **Team Size** TBD 

> **Historical document — the original project proposal (the "north star").** It records the
> initial requirements/scope and is kept for reference; it is **not** a description of the current
> system. For what the agent actually does today see [`FEATURES.md`](../FEATURES.md) (the live,
> evidence-backed feature inventory); for design rationale and the implemented-status record see
> [`plan.md`](plan.md).

## **1. Executive Summary** 

Large Language Model (LLM) inference is rapidly becoming one of the most resource-intensive workloads in modern data centers. Evaluating distributed inference systems requires carefully designed benchmarks that reflect real-world usage patterns—yet crafting such benchmarks today is a specialized skill. Users must reason about request distributions, token lengths, concurrency levels, prefix-sharing ratios, and scheduling policies, then translate these into specific benchmark tool configurations, select the right load generator, and interpret multidimensional results. 

The llm-d project (github.com/llm-d) is a Kubernetes-native distributed LLM inference platform co-founded by Google, IBM Research, Red Hat, NVIDIA, and CoreWeave. Its companion project, llm-d-benchmark, provides automated benchmarking workflows with pluggable load generators, workload profiles, and a Design of Experiments (DOE) framework. However, the current tooling assumes expert knowledge of both the benchmark framework and inference systems. 

This project proposes the development of a Benchmarking Agent: an intelligent, Kubernetes-native service that assists users in designing, executing, and analyzing LLM inference benchmarks. The agent wraps the existing llm-d-benchmark toolchain, acting as an interactive advisor that interviews users about their workload, maps requirements to the correct <scenario, harness, profile> triplet, orchestrates execution as Kubernetes Jobs, and presents structured results. The project is self-contained, exercises core distributed systems concepts, and produces a tangible open-source contribution. 

## **2. Background and Motivation** 

## **2.1 llm-d and llm-d-benchmark** 

llm-d is a Kubernetes-native distributed LLM inference platform providing production-grade serving with KV-cache disaggregation, prefix-aware routing, and workload-aware scheduling. It exposes a series of “well-lit paths”—tested, benchmarked deployment patterns including prefill/decode disaggregation, intelligent inference scheduling, tiered prefix caching, wide expert parallelism, and precise prefix-cacheaware scheduling. 

The llm-d-benchmark repository provides the automation for evaluating these paths. It is organized around three core abstractions: 

- **Scenarios:** A cluster configuration specifying GPU model, LLM, and llm-d parameters (environment file and Helm values). Scenarios include pre-built guides for each well-lit path (e.g., pd-disaggregation.sh, inference-scheduling.sh, tiered-prefix-cache.sh, precise-prefix-cacheaware.sh). 

- **Harnesses:** Pluggable load generators. Currently supported: inference-perf (Kubernetes SIG wgserving), guidellm (vLLM/Red Hat), vLLM’s built-in benchmark scripts, InferenceMAX, and a “nop” harness for model load-time testing. 

- **Workload Profiles:** Benchmark load specifications defining use case, traffic pattern, input/output token distributions, and dataset. Examples include chatbot_synthetic, sanity_random, and various profiles under workload/profiles. 

The reproducibility contract is the triplet _**<scenario, harness, workload profile>**_ , which, combined with the standup/teardown capabilities of llm-d-infra and llm-d-modelservice, fully specifies a reproducible experiment. 

## **2.2 Existing Benchmark Tooling in Detail** 

Each harness has distinct capabilities and configuration surfaces: 

**Harness Origin Key Capabilities** 

|**inference-perf**|Kubernetes SIG wg-serving<br>(Google, IBM, Red Hat)|Poisson/constant load generation, multi-process<br>concurrency, staged QPS ramps, multi-server<br>support (vLLM, SGLang, TGI), standardized<br>metric output, Helm-based K8s deployment|
|---|---|---|
||||
|**guidellm**|vLLM project / Red Hat|Sweep profiles for finding max throughput,<br>constant/Poisson rate profiles, synthetic and<br>HuggingFace dataset support, multimodal<br>benchmarking (image, video, audio),<br>HTML/CSV/JSON reports|
||||
|**vLLM**<br>**benchmarks**|vLLM project|Built-in benchmarks from the vLLM benchmarks/<br>folder; direct integration with vLLM internals and<br>metrics|
||||
|**InferenceMAX**|InferenceMAX project|Focused on maximizing inference throughput;<br>GPU utilization optimization benchmarks|
||||
|**nop (no-op)**|llm-d-benchmark internal|Measures model load times and startup<br>overhead only; useful for cold-start and scaling<br>benchmarks|
||||



llm-d-benchmark also includes two advanced subsystems that are directly relevant to this project: 

- **Design of Experiments (DOE):** A declarative file describing a matrix of standup and run parameters. Each parameter (“factor”) lists target values (“levels”), and the system generates the cross-product of “treatments” to execute automatically. This is the key automation primitive for systematic benchmarking. 

- **Configuration Explorer:** A library that helps find the most cost-effective serving configuration based on hardware specs, workload characteristics, and SLO requirements. Includes a Capacity Planner that determines whether a vLLM configuration is feasible for deployment. 

## **2.3 The Benchmarking Gap** 

Despite this rich tooling, users face a steep learning curve: 

- Selecting the right harness for a given use case (e.g., inference-perf for K8s-native staged load testing vs. guidellm for sweep-based throughput discovery). 

- Translating high-level requirements (“chat app with 500 concurrent users”) into concrete profile parameters: token distributions, QPS targets, prefix patterns, dataset selection. 

- Understanding which well-lit path scenario to benchmark and how workload characteristics interact with system features (e.g., how prefix-sharing ratio affects KV-cache hit rates, or when P/D disaggregation helps vs. hurts). 

- Navigating the DOE system to design parameter sweeps that answer specific questions (e.g., “What is the optimal prefill/decode GPU ratio for my workload shape?”). 

- Interpreting multidimensional results: TTFT, TBT, throughput, P50/P95/P99 latency, KV-cache hit rates, goodput (requests meeting SLOs), and $/M tokens. 

An intelligent agent that bridges this gap would significantly lower the barrier to entry and improve the quality of community benchmarks, while also exercising distributed systems principles in a productionrelevant setting. 

## **3. Project Description** 

## **3.1 System Overview** 

The system consists of three components forming a closed loop from workload design to result analysis. Critically, it does not reinvent the benchmarking stack—it wraps and orchestrates the existing llm-dbenchmark tooling. 

||||
|---|---|---|
|**Component**|**Role**|**Key Responsibilities**|
||||
||||
|**Conversational**<br>**Agent**|Workload Advisor & Profile<br>Builder|Interviews users about use case; maps<br>requirements to harness, profile, and<br>scenario selections; generates DOE<br>experiment files for parameter sweeps;<br>explains trade-offs|
||||
|**Benchmark**<br>**Orchestrator**|K8s Job Lifecycle Manager|Generates K8s Job manifests wrapping llm-<br>d-benchmark’s run.sh; submits, monitors,<br>and manages jobs via K8s API; handles<br>retries, timeouts, log streaming; collects<br>benchmark reports from completed pods|
||||
|**Results Analyzer**|Insights & Comparison Engine|Parses the universal Benchmark Report<br>format and harness-native outputs; extracts<br>TTFT, TBT, throughput, latency percentiles,<br>goodput; compares runs (A/B, sweep<br>analysis); generates structured reports|



## **3.2 Conversational Agent** 

The agent is the user-facing entry point. It uses an LLM (via OpenAI-compatible API—which can itself be served by llm-d) to conduct a structured interview: 

1. **Use-case identification:** Chat, code completion, RAG, batch summarization, agentic pipeline, etc. Maps to well-lit path scenarios (e.g., chat → prefix-cache-aware scheduling, long-context RAG → P/D disaggregation). 

2. **Scale parameters:** Expected concurrency, request rate, burst patterns. Determines load generation mode (Poisson, constant, staged ramp, sweep). 

3. **Token characteristics:** Input/output length distributions, system prompt length and reuse ratio. Directly maps to workload profile parameters and prefix-sharing configuration. 

4. **QoS targets:** TTFT, TBT, P99 latency constraints, throughput floor. Used to filter results and compute goodput. 

5. **Infrastructure context:** GPU type, cluster size, model name. Maps to scenario selection. 

6. **Harness selection:** Based on the above, the agent recommends the appropriate harness (e.g., guidellm sweep for throughput discovery, inference-perf staged ramp for SLO validation). 

The agent then produces one of two outputs: (a) a concrete run.sh invocation with the selected <scenario, harness, profile> triplet, or (b) a DOE experiment file for systematic parameter sweeps. It explains its reasoning and allows users to adjust before execution. 

**Design constraint:** The agent’s knowledge base (workload templates, harness capabilities, parameter mappings, well-lit path heuristics) must be structured as maintainable configuration files, not hard-coded logic. This enables the llm-d community to contribute new workload profiles and heuristics over time. 

## **3.3 Benchmark Orchestrator** 

The orchestrator is the distributed systems core of the project. It manages the lifecycle of benchmark jobs on Kubernetes: 

- **Manifest generation:** Translates agent output into Kubernetes Job specifications. For single runs, this wraps llm-d-benchmark’s run.sh with the appropriate --harness, --workload, and -- methods flags. For DOE sweeps, it generates a Job per treatment from the experiment matrix. 

- **Dependency management:** Ensures the target llm-d stack is healthy before submitting benchmark jobs (health checks against the inference endpoint). Optionally triggers standup.sh if no stack is pre-deployed. 

- **Job monitoring:** Watches Job status via the Kubernetes API (Watch API for event-driven updates). Streams logs in real-time. Detects OOM kills, timeouts, and pod evictions. 

- **Result collection:** Extracts the universal Benchmark Report (JSON) and harness-native output from completed pods. For DOE sweeps, correlates results across treatments. 

- **Fault tolerance:** Configurable retry policies for transient failures. Dead-letter handling for persistently failing treatments in a sweep. Checkpoint/resume for long-running DOE experiments. 

- **Cleanup:** Removes completed Job resources and temporary ConfigMaps. Preserves result artifacts. 

**Design constraint:** The orchestrator must be stateless, using Kubernetes resources (Jobs, ConfigMaps, annotations, labels) as the source of truth. This makes it resilient to orchestrator restarts—a recovering orchestrator can reconstruct state from the cluster. 

## **3.4 Results Analyzer** 

The analyzer processes raw benchmark output and produces structured insights: 

- **Metric extraction:** Parses the standard metrics defined by llm-d-benchmark: throughput (requests/sec, tokens/sec), latency percentiles (P50, P95, P99), time-to-first-token (TTFT), timebetween-tokens (TBT/TPOT), inter-token latency (ITL), KV-cache hit rate, schedule delay, and GPU utilization. 

- **Goodput computation:** Given user-specified SLO constraints (from the agent interview), computes goodput: the fraction of requests meeting all SLO targets. This is a key differentiator from raw throughput. 

- **A/B comparison:** Compares two benchmark runs (e.g., with vs. without prefix-cache-aware scheduling, or different GPU allocations). 

- **DOE analysis:** For parameter sweeps, identifies Pareto-optimal configurations across the treatment matrix (e.g., best throughput at a given latency constraint). Integrates with the existing Configuration Explorer’s Pareto visualization. 

- **Report generation:** Produces JSON-structured results compatible with llm-d-benchmark’s universal Benchmark Report format, plus optional human-readable summaries (via the conversational agent). 

## **4. Distributed Systems Relevance** 

This project exercises core distributed systems concepts in a practical, production-relevant setting: 

|||
|---|---|
|**Concept**|**How It Appears in the Project**|
|||
|||
|**Job Scheduling**|The orchestrator manages distributed job lifecycle via the Kubernetes<br>API—submission, monitoring, retry, and cleanup. For DOE sweeps, it<br>implements parallel job scheduling with configurable concurrency limits<br>across a multi-node cluster.|



|||
|---|---|
|**Resource Management**|Benchmark jobs compete for GPU and CPU resources. The system must<br>handle quotas, node affinity (GPU-type selection), and resource<br>contention gracefully. The orchestrator must ensure benchmark jobs<br>don’t starve the llm-d stack they’re measuring.|
|||
|**Fault Tolerance**|Jobs may fail due to OOM, preemption, or transient errors. The<br>orchestrator implements retry policies, dead-letter queues for DOE<br>treatments, and checkpoint/resume for long-running experiments.|
|||
|**Stateless Design**|The orchestrator stores no local state—all session data lives in<br>Kubernetes resources (Job annotations, ConfigMap data, pod labels). A<br>crashed orchestrator can fully reconstruct its state from the cluster.|
|||
|**API Design**|Clean interfaces between agent, orchestrator, and analyzer enable<br>independent development. The API boundary follows the existing llm-d-<br>benchmark contract: <scenario, harness, profile> in, Benchmark Report<br>out.|
|||
|**Observability**|Collecting, correlating, and presenting metrics from distributed<br>benchmark processes. Real-time log streaming from benchmark pods.<br>Integration with Prometheus/Grafana for live GPU and system metrics.|
|||
|**Distributed Coordination**|For DOE sweeps with parallel treatments, the orchestrator coordinates<br>execution order, manages shared cluster resources, and ensures result<br>consistency across concurrent benchmark pods.|
|||



## **5. Scope and Deliverables** 

## **5.1 Minimum Viable Project (MVP)** 

The MVP demonstrates the end-to-end flow with a constrained but functional scope: 

- Conversational agent handling at least 3 workload archetypes (chat, RAG, batch) and producing valid run.sh invocations with the correct harness and profile selection. 

- Kubernetes-based orchestrator that submits benchmark jobs wrapping llm-d-benchmark’s run.sh, monitors Job completion via Watch API, and collects the universal Benchmark Report. 

- Results parser extracting TTFT, TBT, throughput, and latency percentiles, with A/B comparison of two runs. 

- Working demo using inference-perf as the primary harness against a pre-deployed llm-d stack (or vLLM standalone) on a small cluster (Kind/Minikube for development, lab cluster for final demo). 

## **5.2 Stretch Goals** 

- DOE experiment file generation: Agent produces a full Design of Experiments matrix; orchestrator executes treatments in parallel with configurable concurrency. 

- Multi-harness support: Agent recommends and orchestrates both inference-perf (for SLO validation) and guidellm (for throughput sweep) in a single session. 

- Integration with Configuration Explorer: Use the Capacity Planner to pre-validate configurations before benchmark execution. 

- Goodput computation with SLO-aware filtering of results. 

- Historical result storage (persistent volume or object storage) with trend visualization. 

- Integration with llm-d’s observability stack (Prometheus/Grafana) for live monitoring during benchmark runs. 

- Well-lit path advisor: Agent recommends which well-lit path scenario to benchmark based on workload characteristics (e.g., prefix-heavy chat → precise-prefix-cache-aware, long-context → pd-disaggregation). 

## **5.3 Final Deliverables** 

- Source code in a public GitHub repository with CI/CD (linting, unit tests, integration tests with llmd-inference-sim). 

- Helm chart or Kustomize manifests for single-command deployment. 

- Technical documentation: architecture, API reference, deployment guide, user guide. 

- Final presentation and live demo. 

- If quality is sufficient: upstream contribution to llm-d-benchmark as an agent module PR. Students credited as authors. 

## **6. Proposed Timeline** 

Assuming a 14-week semester: 

||||
|---|---|---|
|**Weeks**|**Phase**|**Activities**|
||||
||||
|**1–2**|**Onboarding & Design**|Study llm-d architecture and benchmark tooling. Run<br>the e2e.sh quickstart. Understand the <scenario,<br>harness, profile> model, DOE system, and<br>Benchmark Report format. Define component APIs.|
||||
|**3–4**|**Agent Prototype**|Implement conversational agent with LLM<br>integration. Build workload-to-config mapping logic<br>for 3 archetypes (chat, RAG, batch). Generate valid<br>run.sh invocations. Unit test against known<br>configurations.|
||||
|**5–7**|**Orchestrator Core**|Build K8s job lifecycle manager: Job manifest<br>generation wrapping run.sh, Watch API-based<br>monitoring, log streaming, result collection from pod<br>volumes/stdout. Retry and timeout policies.<br>Integration tests with Kind cluster.|
||||
|**8–9**|**Results Analyzer**|Implement Benchmark Report parser, metric<br>extraction, A/B comparison logic. Wire end-to-end:<br>agent → orchestrator → analyzer. Validate with real<br>benchmark output from inference-perf runs.|
||||
|**10–11**|**Integration Testing**|End-to-end testing on a Kubernetes cluster with a<br>real llm-d or vLLM stack. Test with multiple<br>harnesses. Bug fixes, edge cases, fault-tolerance<br>hardening (OOM, pod eviction, timeout).|
||||
|**12–13**|**Polish & Stretch**|Documentation, Helm chart, stretch goals (DOE<br>support, multi-harness, Configuration Explorer<br>integration). Upstream PR preparation if applicable.|
||||
|**14**|**Presentation**|Final demo, code freeze, project report submission.|
||||



## **7. Technology Stack** 

|||
|---|---|
|**Layer**|**Technology**|
|**Language**|Python 3.11+ (aligns with llm-d and inference-perf ecosystem)|
|||
|**Agent LLM Backend**|Any OpenAI-compatible API (llm-d endpoint, vLLM, Claude, or local model)|
|||
|**Kubernetes Client**|kubernetes Python client (official) or kr8s for async Watch support|
|||
|**Benchmark Tools**|llm-d-benchmark (run.sh, e2e.sh), inference-perf, guidellm, vLLM<br>benchmarks|
|||
|**API Framework**|FastAPI for the orchestrator REST/WebSocket API|
|||
|**Result Parsing**|llm-d-benchmark universal Benchmark Report (JSON), harness-native<br>formats|
|||
|**Packaging**|Container images (Dockerfile), Helm chart for K8s deployment|
|||
|**Testing**|pytest; Kind cluster for integration; llm-d-inference-sim for mocking|
|||
|**Observability**|Prometheus metrics export, optional Grafana dashboard integration|
|||



## **8. Evaluation Criteria** 

Suggested grading dimensions aligned with distributed systems course objectives: 

1. **Correctness and reliability of K8s job orchestration (40%):** Does the system correctly manage the full job lifecycle? Are edge cases (OOM, timeout, pod eviction) handled? Is the system resilient to orchestrator restarts? 

2. **System design and API quality (25%):** Is the architecture well-decomposed? Are interfaces clean, documented, and aligned with the existing llm-d-benchmark contract? Is the system extensible to new harnesses? 

3. **End-to-end functionality (20%):** Does the complete flow work—from conversation through benchmark execution to structured results? Is the demo convincing on a real cluster? 

4. **Code quality and documentation (15%):** Is the code well-structured, tested, and documented? Could someone else deploy and extend it? Is the Helm chart production-ready? 

## **9. Prerequisites and Resources** 

## **9.1 Student Prerequisites** 

- Familiarity with Kubernetes concepts (Pods, Jobs, Services, ConfigMaps, Watch API). Prior kubectl experience helpful but not required—onboarding in weeks 1–2 covers this. 

- Python proficiency and comfort with REST APIs and async programming. 

- Basic understanding of LLM inference concepts (tokens, latency, throughput). Can be learned during onboarding using llm-d documentation and blog posts. 

## **9.2 Provided Resources** 

- Full access to llm-d documentation, source code, benchmark tool repositories, and blog posts (including the Intelligent Inference Scheduling deep-dive and KV-Cache Wins You Can See). 

- Mentorship from the llm-d benchmark team (weekly check-ins, async support via sigbenchmarking Slack channel). 

- Pre-configured Kubernetes cluster with GPU access for the integration testing phase (weeks 10+), or access to the lab cluster. Development uses Kind/Minikube with llm-d-inference-sim. 

- Access to benchmark experiment data published on the llm-d Google Drive (real performance baselines for validation). 

## **10. Open-Source Contribution Path** 

This project is designed so that high-quality output can be contributed upstream to the llm-d ecosystem: 

- The benchmarking agent can be submitted as a new module in the llm-d-benchmark repository, extending its existing architecture with an intelligent front-end. 

- Workload templates and harness-selection heuristics developed during the project become community resources, benefiting all llm-d-benchmark users. 

- The DOE generation logic (stretch goal) directly enhances the existing Design of Experiments subsystem. 

- Students who contribute will be credited as authors in the repository, acknowledged in sigbenchmarking meetings, and mentioned in any associated blog posts or conference talks. 

_This provides students with a tangible, public portfolio artifact and real-world open-source experience in a project backed by Google, IBM, Red Hat, NVIDIA, and CoreWeave._ 

_llm-d • github.com/llm-d • Open Source under Apache 2.0_ 
