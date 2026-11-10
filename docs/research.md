# Research basis

Every algorithm in trimset cites the work it rests on. This page maps each part of the
product to its sources, and records how each citation was verified.

**Checked 2026-11-10.**
- arXiv entries: titles, authors and dates read from the arXiv API (`export.arxiv.org`).
- DOI entries: read from the Crossref API.
- The three papers with no DOI: checked against publisher or proceedings indexes.

Nothing here is cited from memory alone.

## 1. What each part uses

| Part (requirement) | Method in v0.1 | Sources |
|---|---|---|
| Calibration fits (FR-16) | Temperature scaling; Platt scaling (with the numerically stable fit); isotonic regression (pool-adjacent-violators). Chosen per group by cross-validated log loss. | Guo 2017; Platt 1999; Lin 2007; Zadrozny 2002; Niculescu-Mizil 2005 |
| Calibration metrics (FR-17) | Log loss, Brier, ECE with equal-width and equal-mass bins, reliability-diagram data | Naeini 2015; Guo 2017; Kumar 2019 |
| Policy thresholds with a guarantee (FR-21 to FR-23) | Learn-then-Test: one hypothesis per candidate threshold, exact binomial p-values, fixed-sequence testing; a selection-conditioned risk such as P(bad \| accepted) | Angelopoulos 2021 (LTT); Clopper 1934; Priye 2026 (exact-binomial certification and availability); Geifman 2017; Chow 1970 |
| Feasibility and "how many labels are needed" (FR-23, FR-24) | Exact binomial inversion; closed-form lower bound on the abstention needed when base risk exceeds the target | Priye 2026; Kotte 2026 |
| Escalation band and cost targets (FR-20, FR-21) | Two thresholds (reject option); expected-cost minimisation (reported with no guarantee) | Chow 1970; Elkan 2001; Madras 2018; Mozannar 2020; Chen 2023 |
| Under shift (FR-25) | Refit on a recent window; the policy says "adaptive, no guarantee". ACI-style target updates stay in `research/`, because they missed their target under shift in recent work. | Gama 2014; Gibbs 2021; Kotte 2026 |
| Live check of an active policy (FR-40) | Anytime-valid confidence sequence on the realised false-pass rate, using matured labels only | Howard 2018; Khosravi 2026; Žliobaitė 2010 |
| Label-free drift (FR-38) | Black-box shift detection on output probabilities: PSI and Jensen-Shannon as effect sizes, permutation-test p-values | Rabanser 2019; Lipton 2018; Lin 1991; Yurdakul 2018; Phipson 2010 |
| Shadow comparison (FR-46) | Exact McNemar test (binomial on discordant pairs) | McNemar 1947 |
| Audit sampling (FR-14, FR-37) | A random share of decisions is labeled regardless of the action, so every action path has unbiased labels | Lakkaraju 2017 |
| Delayed labels (FR-11, FR-13, FR-40) | Only "matured" decisions (older than the label delay) count as labeled; rule labels fill the "nothing happened" case | Žliobaitė 2010; Gama 2014 |

**Calibration is not what gives the guarantee.** The threshold guarantee comes from
Learn-then-Test on a split the calibration map never saw, so it holds for any score.
Calibration makes the probabilities readable as frequencies and makes the thresholds
more efficient.

## 2. Bibliography

### Foundations

| Key | Reference | Link | Verified |
|---|---|---|---|
| Platt 1999 | J. C. Platt. Probabilistic outputs for support vector machines and comparisons to regularized likelihood methods. In *Advances in Large Margin Classifiers*, MIT Press. | (no DOI) | Publisher indexes. Sources give 1999 or 2000 for the book; we cite 1999 as is customary. |
| Lin 2007 | H.-T. Lin, C.-J. Lin, R. C. Weng. A note on Platt's probabilistic outputs for support vector machines. *Machine Learning*, 2007. | [doi:10.1007/s10994-007-5018-6](https://doi.org/10.1007/s10994-007-5018-6) | Crossref |
| Zadrozny 2002 | B. Zadrozny, C. Elkan. Transforming classifier scores into accurate multiclass probability estimates. KDD 2002. | [doi:10.1145/775047.775151](https://doi.org/10.1145/775047.775151) | Crossref |
| Niculescu-Mizil 2005 | A. Niculescu-Mizil, R. Caruana. Predicting good probabilities with supervised learning. ICML 2005. | [doi:10.1145/1102351.1102430](https://doi.org/10.1145/1102351.1102430) | Crossref |
| Guo 2017 | C. Guo, G. Pleiss, Y. Sun, K. Q. Weinberger. On calibration of modern neural networks. ICML 2017. | [arXiv:1706.04599](https://arxiv.org/abs/1706.04599) | arXiv API |
| Naeini 2015 | M. P. Naeini, G. Cooper, M. Hauskrecht. Obtaining well calibrated probabilities using Bayesian binning. AAAI 2015. | [doi:10.1609/aaai.v29i1.9602](https://doi.org/10.1609/aaai.v29i1.9602) | Crossref |
| Kumar 2019 | A. Kumar, P. Liang, T. Ma. Verified uncertainty calibration. NeurIPS 2019. | [arXiv:1909.10155](https://arxiv.org/abs/1909.10155) | arXiv API |
| Chow 1970 | C. Chow. On optimum recognition error and reject tradeoff. *IEEE Trans. Information Theory*, 1970. | [doi:10.1109/TIT.1970.1054406](https://doi.org/10.1109/TIT.1970.1054406) | Crossref |
| Geifman 2017 | Y. Geifman, R. El-Yaniv. Selective classification for deep neural networks. NeurIPS 2017. | [arXiv:1705.08500](https://arxiv.org/abs/1705.08500) | arXiv API |
| Angelopoulos 2021 | A. N. Angelopoulos, S. Bates, E. J. Candès, M. I. Jordan, L. Lei. Learn then Test: calibrating predictive algorithms to achieve risk control. | [arXiv:2110.01052](https://arxiv.org/abs/2110.01052) | arXiv API |
| Angelopoulos 2022 | A. N. Angelopoulos, S. Bates, A. Fisch, L. Lei, T. Schuster. Conformal risk control. | [arXiv:2208.02814](https://arxiv.org/abs/2208.02814) | arXiv API |
| Madras 2018 | D. Madras, T. Pitassi, R. Zemel. Predict responsibly: improving fairness and accuracy by learning to defer. NeurIPS 2018. | [arXiv:1711.06664](https://arxiv.org/abs/1711.06664) | arXiv API |
| Mozannar 2020 | H. Mozannar, D. Sontag. Consistent estimators for learning to defer to an expert. ICML 2020. | [arXiv:2006.01862](https://arxiv.org/abs/2006.01862) | arXiv API |
| Chen 2023 | L. Chen, M. Zaharia, J. Zou. FrugalGPT: how to use large language models while reducing cost and improving performance. | [arXiv:2305.05176](https://arxiv.org/abs/2305.05176) | arXiv API |
| Elkan 2001 | C. Elkan. The foundations of cost-sensitive learning. IJCAI 2001, pp. 973–978. | (no DOI) | Proceedings index ([mlanthology](https://mlanthology.org/ijcai/2001/elkan2001ijcai-foundations)) |
| Gibbs 2021 | I. Gibbs, E. Candès. Adaptive conformal inference under distribution shift. NeurIPS 2021. | [arXiv:2106.00170](https://arxiv.org/abs/2106.00170) | arXiv API |
| Rabanser 2019 | S. Rabanser, S. Günnemann, Z. C. Lipton. Failing loudly: an empirical study of methods for detecting dataset shift. NeurIPS 2019. | [arXiv:1810.11953](https://arxiv.org/abs/1810.11953) | arXiv API |
| Lipton 2018 | Z. C. Lipton, Y.-X. Wang, A. Smola. Detecting and correcting for label shift with black box predictors. ICML 2018. | [arXiv:1802.03916](https://arxiv.org/abs/1802.03916) | arXiv API |
| Gama 2014 | J. Gama, I. Žliobaitė, A. Bifet, M. Pechenizkiy, A. Bouchachia. A survey on concept drift adaptation. *ACM Computing Surveys*, 2014. | [doi:10.1145/2523813](https://doi.org/10.1145/2523813) | Crossref |
| Žliobaitė 2010 | I. Žliobaitė. Change with delayed labeling: when is it detectable? ICDM Workshops 2010. | [doi:10.1109/ICDMW.2010.49](https://doi.org/10.1109/ICDMW.2010.49) | Crossref |
| Clopper 1934 | C. J. Clopper, E. S. Pearson. The use of confidence or fiducial limits illustrated in the case of the binomial. *Biometrika*, 1934. | [doi:10.1093/biomet/26.4.404](https://doi.org/10.1093/biomet/26.4.404) | Crossref |
| McNemar 1947 | Q. McNemar. Note on the sampling error of the difference between correlated proportions or percentages. *Psychometrika*, 1947. | [doi:10.1007/BF02295996](https://doi.org/10.1007/BF02295996) | Crossref |

### Added in Phase 2 (not in the brief)

| Key | Reference | Link | Verified | Why |
|---|---|---|---|---|
| Lakkaraju 2017 | H. Lakkaraju, J. Kleinberg, J. Leskovec, J. Ludwig, S. Mullainathan. The selective labels problem. KDD 2017. | [doi:10.1145/3097983.3098066](https://doi.org/10.1145/3097983.3098066) | Crossref | Audit sampling (FR-14) |
| Howard 2018 | S. R. Howard, A. Ramdas, J. McAuliffe, J. Sekhon. Time-uniform, nonparametric, nonasymptotic confidence sequences. | [arXiv:1810.08240](https://arxiv.org/abs/1810.08240) | arXiv API | Live policy checks that stay valid however often they run (FR-40) |
| Lin 1991 | J. Lin. Divergence measures based on the Shannon entropy. *IEEE Trans. Information Theory*, 1991. | [doi:10.1109/18.61115](https://doi.org/10.1109/18.61115) | Crossref | Jensen-Shannon divergence (FR-38) |
| Phipson 2010 | B. Phipson, G. K. Smyth. Permutation p-values should never be zero. *Stat. Appl. Genet. Mol. Biol.*, 2010. | [doi:10.2202/1544-6115.1585](https://doi.org/10.2202/1544-6115.1585) | Crossref | Correct p-values from sampled permutations (FR-38) |
| Yurdakul 2018 | B. Yurdakul. Statistical properties of population stability index. PhD dissertation, Western Michigan University, 2018. | [ScholarWorks 3208](https://scholarworks.wmich.edu/dissertations/3208/) | University repository | PSI has no canonical origin paper. This is the study of its statistical behaviour. We use permutation tests instead of PSI rule-of-thumb cut-offs. |
| Lin 2007 | (see Foundations) | | | Stable Platt fit |
| Niculescu-Mizil 2005 | (see Foundations) | | | Isotonic needs more data than Platt; supports choosing the method by cross-validation |

### Recent work (2025–2026)

All verified through the arXiv API. Each row says what trimset takes from the paper, and when.

| Key | Reference | What we take | When |
|---|---|---|---|
| Priye 2026 | P. Priye, Y. Wang, H. Ling, M. Chaykowsky. Available guardrails: certifying selective prediction across ML systems. [arXiv:2609.22048](https://arxiv.org/abs/2609.22048) | Certification by exact-binomial inversion, as our compiler does. "Availability" (whether finite data can certify a gate at all) becomes our "N more labels needed" message. Splitting the error budget across units (for example per option) under a familywise bound. | v0.1 |
| Kotte 2026 | V. Kotte. When can conformal risk control certify LLM outputs? Bounds, impossibility, and adaptation for structured generation. [arXiv:2606.29054](https://arxiv.org/abs/2606.29054) | A closed-form feasibility test: when base risk μ > α, at least (μ−α)/(M−α) of inputs must be escalated. Its finding that static CRC and ACI missed their target under shift is why trimset drops the guarantee under shift instead of adapting silently. | v0.1 |
| Wang 2025 | Z. Wang et al. LEC: linear expectation constraints for selection-conditioned risk control in selective prediction and routing systems. [arXiv:2512.01556](https://arxiv.org/abs/2512.01556) | Selection-conditioned risk (error given accepted) is our false-pass definition. Its two-model routing extension fits UC-3 (model routing). It controls risk in expectation, not with high probability, so it would be a second guarantee type. | v0.2 candidate |
| Bai 2026 | T. Bai, Y. Jin. Conformal selective prediction with general risk control (SCoRE). [arXiv:2603.24704](https://arxiv.org/abs/2603.24704) | E-value based selective risk control, any bounded risk, extensions under shift. A candidate replacement for the binomial tests when risks are not 0/1. | v0.2 / `research/` |
| Joshi 2025 | S. Joshi, Y. Sun, H. Hassani, E. Dobriban. MultiRisk: multiple risk control via iterative score thresholding. [arXiv:2512.24587](https://arxiv.org/abs/2512.24587) | Several budgets with priorities at once. | v0.2 |
| Salem 2026 | M. Salem, L. Böhm, D. Pontes, A. Ferrugem. Hierarchical group-conditional conformal risk control for selective prediction in language models. [arXiv:2607.24562](https://arxiv.org/abs/2607.24562) | Per-slice guarantees over a hierarchy of tags, with a Bonferroni correction and a leaf-first fallback. | v0.2 |
| Khosravi 2026 | H. Khosravi, X. Huo. Conformal selective acting: anytime-valid risk control for RLVR-trained LLMs. [arXiv:2605.20270](https://arxiv.org/abs/2605.20270) | An anytime-valid selective-risk certificate on a stream (an e-process per threshold). It shapes our live policy check, and is a candidate for anytime-valid thresholds. | v0.1 monitoring idea; method v0.2 |
| Badshah 2026 | S. Badshah, A. Emami, H. Sajjad. SCOPE: selective conformal optimized pairwise LLM judging. [arXiv:2602.13110](https://arxiv.org/abs/2602.13110) | Error control among accepted judgments for pairwise LLM judges. Relevant when an LLM judge supplies outcomes (UC-3) or compares shadow models. | `research/` |
| Yaldiz 2026 | D. N. Yaldiz et al. Balancing classification and calibration performance in decision-making LLMs via calibration aware reinforcement learning. [arXiv:2601.13284](https://arxiv.org/abs/2601.13284) | Evidence that RL-tuned decision models can be badly overconfident. This motivates post-hoc calibration for every group. | Context |

The brief's title for 2606.29054 is shorter than the published one; the full title is
used above.

## 3. Published results to reproduce (golden tests)

The brief asks for each method to reproduce one published result. Phase 6 picks the exact
figures; these are the candidates. No numbers are quoted here until we have reproduced
them.

| Method | Candidate result to reproduce |
|---|---|
| Exact binomial bounds | Clopper-Pearson interval values, against `scipy.stats.binomtest(...).proportion_ci(method="exact")` |
| Temperature scaling | Guo 2017's finding that one temperature removes most of the ECE of an overconfident model, shown on synthetic overconfident logits |
| Platt and isotonic | Niculescu-Mizil 2005's finding that Platt wins with little calibration data and isotonic wins with more |
| Learn-then-Test | The risk-control experiment in Angelopoulos 2021: realised risk ≤ α in at least 1−δ of trials |
| Drift tests | Rabanser 2019's finding that univariate tests on model outputs detect planted shifts, on synthetic data |
| McNemar | A worked example from the original paper or a standard text, against scipy |

## 4. Research track (`research/`)

What appears to be unpublished: risk control for **typed decision models** (noul, choice,
score) in production, with **outcomes that arrive late** and **labels that depend on the
action taken**. Planned experiments, all on the synthetic generator with fixed seeds:

1. **Coverage under delay:** does the Learn-then-Test guarantee hold when only matured labels are used, under different delay distributions? (This is also NFR-09.)
2. **Audit sampling against selective labels:** how small an audit share keeps false-pass estimates unbiased when blocked items never get labels?
3. **Shift:** recent-window refits against ACI-style target updates against e-value methods (SCoRE, CSA), measuring target violations and the share automated.
4. **Availability:** how many labels each use case needs before a guarantee is possible, following Priye 2026.

## 5. Flags

- **Platt 1999:** sources disagree on the year (1999 or 2000). The content is not in doubt.
- **PSI:** no canonical origin paper was found. We cite a study of its statistical properties, and do not use its folk cut-offs (0.1 / 0.25) as tests.
- **Ollama 0.40+ serving decision models:** this comes from the brief. It is a product fact, not research, and is checked in Phase 3.
