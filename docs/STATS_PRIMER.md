# The Stats Behind Verdict — A Primer for Engineers

A plain-language explanation of current Monitor statistics, judge calibration,
and related historical drift diagnostics. The sections below distinguish the
current alert path from older fixed-window methods and general examples.

You should be able to read this in 30-40 minutes and come away understanding
the statistics behind Monitor, semantic drift, and judge calibration.

---

## Contents

1. [The fundamental question we keep asking](#1-the-fundamental-question-we-keep-asking)
2. [Distributions, samples, and how to compare them](#2-distributions-samples-and-how-to-compare-them)
3. [The significance test: Fisher's exact & Mann-Whitney U — does this group differ from that group?](#3-the-significance-test-fishers-exact-binary-and-mann-whitney-u-continuous)
4. [p-values — and what they actually mean](#4-p-values--and-what-they-actually-mean)
5. [Effect sizes — "yes, but how much?"](#5-effect-sizes--yes-but-how-much)
6. [Cliff's delta — a historical effect size](#6-cliffs-delta--a-historical-effect-size)
7. [Cohen's d — kept for legacy reasons](#7-cohens-d--kept-for-legacy-reasons)
8. [Wasserstein distance — Earth Mover's Distance](#8-wasserstein-distance--earth-movers-distance)
9. [Population Stability Index — PSI](#9-population-stability-index--psi)
10. [Multi-testing and Benjamini-Hochberg correction](#10-multi-testing-and-benjamini-hochberg-correction)
11. [Inter-rater agreement: do two judges agree?](#11-inter-rater-agreement-do-two-judges-agree)
12. [Gwet's AC1 — chance-corrected agreement](#12-gwets-ac1--chance-corrected-agreement)
13. [How it all fits together in Verdict](#13-how-it-all-fits-together-in-verdict)
14. [Quick reference card](#14-quick-reference-card)

---

## 1. The fundamental question we keep asking

Statistical monitoring answers one question:

> *"Did something change?"*

Here, "change" means a measured difference between eligible reference and
current evidence. Verdict also profiles individual runs and investigates
recorded failures without making a statistical drift claim.

The comparison uses a reference cohort and a later eligible cohort. The hard
part is deciding whether an observed difference is compatible with sampling
variation, after accounting for evidence coverage and repeated comparisons.

That's the question statistics answers. The rest of this primer is just the specific tools that answer different versions of it.

---

## 2. Distributions, samples, and how to compare them

Before we can ask "did it change," we need a way to talk about "it."

**A distribution** is the answer to: "if I look at a bunch of LLM responses on a particular task, how often does each quality level show up?" It's a histogram, conceptually. For our PASS/FAIL judge, the distribution might be:

- 90% PASS, 10% FAIL → "this model is usually good on this task"
- 50% PASS, 50% FAIL → "this model is unreliable on this task"
- 100% observed PASS, 0% observed FAIL → no failure in this sample; the
  underlying failure rate remains uncertain

The distribution captures the underlying tendency of the system. A response is a single sample from that distribution.

**The catch**: you never see the true distribution. You only see samples. Maybe the model is *truly* 80% PASS, but the 17 responses you sampled yesterday happened to be 15 PASS and 2 FAIL (88%). Today you sample 18 responses and get 11 PASS, 7 FAIL (61%). Did the underlying distribution change? Or did you just get unlucky today?

Statistical tests help evaluate that question when the compared units and
coverage satisfy the method's assumptions.

### The two-distribution comparison

The current Monitor comparison has:

- **Reference window** — the frozen earlier eligible samples
- **Current window** — the later prospective eligible samples

We ask: "are these two windows samples from the same underlying distribution, or different ones?"

Current Monitor uses Fisher's exact test for eligible binary metrics. The
Mann-Whitney discussion below describes a continuous-score method used by
older fixed-window analysis, not a current Monitor alert path.

**Monitor implementation note:** Monitor freezes either count-based or explicit
event-time membership using the captured trace's event time, then collects
prospective non-overlapping cohorts. Activation stores an event-time boundary
and starts with an empty current bucket; an older event imported later is not
reclassified as post-activation traffic. A selected
evaluator may finalize its pending results while a fixed current cohort remains
open; no comparison is made until those results are terminal. Changed or
deleted pending evidence requires a new preview rather than rewriting frozen
facts. With a
provider/model or reviewed-cluster facet, the tested family contains each
eligible `(group, metric)` cell. Minimum sample counts apply inside each cell,
and Benjamini-Hochberg correction covers the complete family for that look.
New traffic is fully projected through a pinned cluster version before its
monitor membership is saved, without refitting. Clustering is optional; without
a facet, Monitor compares all eligible traffic. Unassigned or new groups are
coverage evidence, not pooled observations. Fixed-window `DriftRun` and
`DriftSignal` records created by older releases remain readable through the
Python storage API. They are excluded from the dashboard, and
`verdict-pipeline` no longer creates them.

The **Logical session (descriptive)** preview is intentionally outside this
inferential procedure. It groups native Agent runs only by explicit
`AgentRun.session_id` and reports reference/current rates, raw effects, and
coverage as of the preview. Verdict does not yet observe authoritative session
finalization or establish independence between those units, so it computes no
Fisher test, adjusted p-value, alert decision, or prospective activation for
that view.

---

## 3. The significance test: Fisher's exact (binary) and Mann-Whitney U (continuous)

The familiar **t-test** compares means. Monitor's current metrics are binary
(0 or 1), so it instead uses an exact 2×2 test for two rates.

We pick the significance test by data type:

- **Binary PASS/FAIL dimensions (the common case)** → **Fisher's exact test**. Comparing two pass rates uses a 2×2 table: (pass vs fail) × (current vs reference). Fisher's exact computes a null p-value without a large-sample approximation for that table. The inferential reading still needs appropriate, independent sampling units and the declared comparison design.
- **Ordinal / continuous scores in older fixed-window analysis** → **Mann-Whitney U** (below). Current Monitor does not activate alerts from this path.

The rest of this section explains the rank-based historical diagnostic.
Fisher's exact test is a separate test for the current binary 2×2 tables.

**Mann-Whitney U** (also called the Wilcoxon rank-sum test) compares ranks
without a normal-distribution requirement. It still requires an appropriate
sampling design and independent units for a usual inferential interpretation.

### How it works, intuitively

Imagine you pool both samples together and rank them from smallest to largest:

```
Baseline: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]      (17 values, all 1)
Current:  [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]   (11 ones + 7 zeros)
```

When you pool them and rank: the 7 zeros from "current" come first (lowest values), then the 28 ones from both groups (all tied).

Mann-Whitney asks: **does the average rank of group A differ from what we'd expect if both groups were drawn from the same population?**

If the two samples came from the same distribution, you'd expect their ranks to be intermingled — half of group A's values should be roughly in the lower half of the combined ranks, half in the upper. But if one group is consistently smaller (like our "current" with all those zeros), its ranks are systematically lower than expected. That's the U statistic.

### The U statistic

For two samples of sizes n₁ and n₂:

- The expected average rank under "same distribution" is (n₁+n₂+1)/2 for each group.
- Compute the actual sum of ranks for each group.
- The difference, scaled appropriately, gives the U statistic.
- If U is very different from what's expected by chance, the samples likely come from different distributions.

You don't need to compute U by hand. `scipy.stats.mannwhitneyu` does it. What you care about is the **p-value** it returns.

### Why these two tests, and not a t-test

- Current Monitor metrics are binary PASS/FAIL or other Boolean observations,
  so a 2×2 test matches the recorded outcomes directly.
- For binary outcomes, Mann-Whitney degrades into a heavily-tied rank test that's only a weaker proxy for a two-proportion comparison — so we use **Fisher's exact test**, which answers exactly that question without approximation. Verdict's dependency-free implementation computes the two-sided p-value from the 2×2 (pass/fail × current/baseline) table and is differentially checked against SciPy.
- For ordinal/continuous scores, Mann-Whitney compares ranks, but ties and
  dependence still need attention. It is included here to explain historical
  diagnostics, not the current Monitor decision.
- Current Monitor pairs Fisher's test with the **absolute difference in binary
  rates**. Cliff's δ below explains a historical effect-size measure.

---

## 4. p-values — and what they actually mean

The p-value is the single most-misunderstood statistic in all of science. Let's nail it down.

The p-value answers a *specific* question:

> *"If the two samples came from the same underlying distribution — if the apparent difference were pure random noise — what's the probability of seeing data this extreme or MORE extreme just by chance?"*

A p-value of 0.005 means: "If there were no real difference, there's only a 0.5% chance we'd see this pattern. So either we got incredibly unlucky, or there's actually a difference."

By convention, p < 0.05 is sometimes called statistically significant. That
does not by itself establish a real or operationally important change.

### The intuition pump

Flip a coin 100 times. You get 60 heads. Is the coin biased?

- Under "the coin is fair" (the *null hypothesis*), the chance of getting 60+ heads in 100 flips is about 2.8%.
- That is evidence against a fair coin under this one-sided example, not proof
  of bias from one experiment.
- The 2.8% is the p-value.

If you got 52 heads instead, the one-sided p-value is about 0.382. That result
does not reject the fair-coin null at a 0.05 threshold.

### What p-value is NOT

The single most common mistake: **p-value is not "the probability the null hypothesis is true."**

- A p-value of 0.05 does NOT mean "there's a 5% chance the samples come from the same distribution."
- It means "IF the samples came from the same distribution, there'd be a 5% chance of seeing data this extreme."

This is the difference between P(data | null) and P(null | data). Different things. Bayesian methods give you the latter; frequentist p-values give you the former. We use frequentist methods, so when we say "p < 0.05" we mean the data is unlikely under the null, NOT that the null is unlikely given the data.

### The threshold

The "p < 0.05 = significant" cutoff comes from R. A. Fisher's 1925 textbook. It's a convention, not a law of nature. More-conservative thresholds (p < 0.01, p < 0.001) are appropriate for higher-stakes decisions.

`MonitorPolicy` defaults to a nominal `p_threshold` of **0.05** and a minimum
absolute rate difference of **0.10**. A prospective comparison uses a smaller
threshold from quadratic alpha spending across repeated looks. The nominal
threshold applies to a historical preview, which is not an alert decision.

### What our drift detector does with p-values

For each eligible Monitor group and binary metric, the current path computes
a two-sided Fisher p-value. It then:

1. Adjust the p-value for multiple comparisons (see §10 below)
2. Compare with the threshold for that look
3. Require at least the policy's minimum absolute rate difference before an alert

A p-value alone does not tell you how much changed. That is what an effect size
reports; neither measure repairs missing or dependent evidence.

---

## 5. Effect sizes — "yes, but how much?"

Here's the dirty secret of p-values: with enough data, you can detect *any* difference as "significant," no matter how trivially small.

Imagine two distributions where the means differ by 0.001. With a million samples, p-value will be tiny, and you'd reject the null and say "they're different!" But practically, who cares — 0.001 is irrelevant.

That's where **effect size** comes in. An effect size answers:

> *"How big is the difference, in practical terms?"*

p-value says: "how surprising is this evidence under the null?" → a probability, not a yes/no diagnosis.
Effect size says: "how big is the signal?" → magnitude.

You need BOTH. p-value without effect size lets trivial differences trigger alarms. Effect size without p-value lets random fluctuations look meaningful.

Current Monitor's default policy requires:
- **BH-adjusted p ≤ the threshold for that look** (nominally 0.05 before
  prospective alpha spending), and
- **absolute current-minus-reference rate ≥ 0.10**.

Both gates have to pass for a signal to fire.

There are many different effect sizes — Cohen's d, Cliff's δ, Glass's Δ, Hedges' g, the list goes on. They differ in what they assume about the data and how they're scaled. Let's look at the two we care about.

---

## 6. Cliff's delta — a historical effect size

**Cliff's δ** (Cliff, 1996) is a rank-based effect size. It can accompany
Mann-Whitney U without a normal-distribution assumption, but neither statistic
removes the need for an appropriate sampling design.

### The definition

For two samples A and B:

> Cliff's δ = P(X₁ > X₂) − P(X₁ < X₂)
>
> where X₁ is randomly drawn from A and X₂ is randomly drawn from B.

In plain English: "if I randomly pick one value from A and one from B, what's the probability A's value is bigger, minus the probability A's value is smaller?"

Range: **−1 to +1**.

- **δ = +1**: every value in A is bigger than every value in B
- **δ = −1**: every value in A is smaller than every value in B
- **δ = 0**: they're indistinguishable

### Concrete example

Consider a binary PASS/FAIL dimension before and after a quality shift:

- **Baseline (pre-regression)**: 17 PASSes, 0 FAILs → all 1s
- **Current (post-regression)**: 11 PASSes, 7 FAILs → 11 ones, 7 zeros

Cliff's δ calculation:

- P(current > baseline): 0% — current is 0 or 1, baseline is always 1, so current can never *exceed* baseline.
- P(current < baseline): the 7 zeros in current vs the 17 ones in baseline → 7 × 17 = 119 "current < baseline" pairs out of 18 × 17 = 306 total pairs → 38.9%.
- Cliff's δ = 0% − 38.9% = **−0.389**

That's a "medium-large" effect by Romano et al. 2006 thresholds:
- |δ| < 0.147 → negligible
- 0.147 ≤ |δ| < 0.33 → small
- 0.33 ≤ |δ| < 0.474 → medium
- |δ| ≥ 0.474 → large

That is a medium-to-large effect size, which matches the intuitive "this pass
rate dropped materially" reading.

### How the historical pipeline interpreted Cliff's delta

- **Pairs with Mann-Whitney U.** Both are non-parametric. Same family.
- **Bounded in [−1, +1].** Easy to interpret. Easy to threshold.
- **Robust to outliers and weird distributions.** Doesn't break on binary data.
- **Directly interpretable.** "The probability your post-regression response is worse than a pre-regression response is 39 percentage points higher than the reverse."

The older fixed-window drift pipeline used Cliff's δ for effect-size gating.
Current Monitor uses a binary rate difference.

---

## 7. Cohen's d — kept for legacy reasons

**Cohen's d** (Cohen, 1969) is THE most famous effect size. Almost every paper you'll read reports it. But it has a hidden assumption: **it assumes the underlying distributions are roughly normal** (bell-curve shaped).

### The formula

> Cohen's d = (mean of A − mean of B) / pooled standard deviation

It's the difference in means, expressed in standard-deviation units.

### Cohen's thresholds

- |d| ≈ 0.2 → small effect
- |d| ≈ 0.5 → medium effect
- |d| ≈ 0.8 → large effect

These are everywhere in social-science literature, and people are familiar with them.

### Why it's wrong for our data

Our judge produces binary PASS/FAIL scores (0 or 1). Binary data is *not normally distributed* — it's about as un-normal as you can get. So Cohen's d's magnitude isn't trustworthy on our data. You'll see numbers like d = −1.08 when the underlying distribution change is much milder.

That is why the legacy fixed-window `DriftSignal` output reported Cohen's d but
gated on Cliff's δ instead. Current Monitor comparisons use a directly
interpretable rate difference for binary metrics.

### When Cohen's d would be appropriate

For a future continuous-score method, a standardized mean difference might be
useful if its sampling assumptions hold. This is not a current Monitor path.

---

## 8. Wasserstein distance — Earth Mover's Distance

Mann-Whitney U tells you "are these distributions different?" Cliff's δ tells you "how different in the order sense?" **Wasserstein distance** tells you "how much would you have to move/reshape one distribution to make it look like the other?"

### The intuition

Imagine two distributions as piles of dirt. Wasserstein distance is the minimum amount of work (dirt × distance moved) required to reshape one pile into the other. Hence the nickname "Earth Mover's Distance."

For two distributions of binary 0/1 data:

- If both are 80% ones / 20% zeros, the piles look the same — distance is 0.
- If one is 80% ones and the other is 50% ones, you need to "move" 30% of the mass from "1" to "0" — over a distance of 1. So Wasserstein distance ≈ 0.30.

In the binary example above, the Wasserstein distance is exactly the amount of
mass that shifted from PASS to FAIL.

### Why older fixed-window analysis reported it alongside Mann-Whitney

- **Directly interpretable.** In the example above, 30% of probability mass
  moves from 1 to 0.
- **Symmetric and metric.** It's a real distance function in mathematical terms; satisfies triangle inequality.
- **Common in ML monitoring.** Useful as a secondary distance-based view of
  distribution movement.

Older fixed-window analysis computed this diagnostic. It does not activate a
current Monitor alert.

### Limits

- For binary data, it's nearly redundant with Cliff's δ (they capture the same shift).
- More valuable on continuous quality scores.
- Still report it as a secondary confirmatory signal.

---

## 9. Population Stability Index — PSI

PSI is a distribution-shift diagnostic in older fixed-window analysis. It
compares categorical or binned distributions; it is not a current Monitor
alert gate.

### The formula

For each bin, compute:

> Contribution = (current % − baseline %) × ln(current % / baseline %)

Then sum across all bins. The result is PSI.

### Interpretation

Common heuristic ranges, not statistical significance tests:

- **PSI < 0.1**: a small diagnostic value under this convention
- **0.1 ≤ PSI < 0.25**: a moderate diagnostic value to inspect
- **PSI ≥ 0.25**: a larger diagnostic value to inspect

### Why we use it

- Captures distributional shift on binned data.
- Easy to compute and interpret.

### Discrete and constant data

The implementation uses one category bin per distinct value when there are few
unique values, including binary PASS/FAIL. This avoids empty linear bins and
still detects a shift from an all-PASS baseline to a mixed current window. If
both windows contain the same single constant value, PSI is correctly zero. For
continuous data it uses baseline-driven linear edges; a constant continuous
baseline remains a limitation, so read PSI alongside Wasserstein and the
older fixed-window tests rather than as an independent alert.

---

## 10. Multi-testing and Benjamini-Hochberg correction

Here's a subtle problem that bites every statistics-naïve product.

**The problem.** With 100 valid null p-values at a per-test threshold of 0.05,
the expected number of raw false rejections is five. Repeating comparisons
creates additional opportunities for false rejections. That arithmetic is an
illustration of uncorrected tests, **not** a forecast of Verdict alerts:
Monitor also applies within-look adjustment, prospective alpha spending,
minimum effect size, and evidence-eligibility gates.

This is the **multiple comparisons problem**.

### The Bonferroni approach

The classic fix: divide the family threshold by the number of tests. With 100
valid null p-values and family-wise error target 5%, require each p-value to
pass 0.0005 (= 0.05/100). This is **Bonferroni correction**: it bounds the
probability of at least one false rejection in that declared family.

That stricter threshold can reduce power when many hypotheses are tested.

### The Benjamini-Hochberg approach

**Benjamini-Hochberg** (1995) targets a different quantity: the **expected
false-discovery proportion** among rejected hypotheses in one declared family,
under its p-value and dependence assumptions. A chosen level does not
guarantee that the realized fraction of alerts is below that level in any
particular comparison, nor does one application cover repeated future looks.

The procedure:

1. Rank all your p-values from smallest to largest.
2. For each p-value at rank k out of N, compute its adjusted threshold: `(k/N) × α`.
3. The rank threshold grows with k; no rank is decided in isolation.
4. Find the largest k where p_k ≤ (k/N) × α. Everything at rank ≤ k is "significant."

### Why use BH for a declared family

- Much less conservative than Bonferroni.
- Targets expected false-discovery proportion for the declared family under
  the method's assumptions.
- Can retain more power than Bonferroni when many signals are present.

### Our application

When Monitor compares N eligible group/metric pairs, it applies BH to those N
p-values together. The declared family is one reviewed comparison: every
scored hypothesis that could alert in that look.

If a future caller mixes binary and continuous windows, Fisher's-exact and
Mann-Whitney p-values remain in that same family. They test different data types,
but each is a valid null p-value for a simultaneously alertable hypothesis.
Splitting the family by test implementation would make the correction change as
data types change and would no longer match the product question, "which cells
alerted in this run?" Today the production judge emits binary PASS/FAIL scores,
so its scored drift path uses Fisher's exact throughout and the mixed-test
distinction is normally inactive.

For 50 eligible groups × 5 metrics = 250 tests at a **single-look** level of
0.05, the rank-one cutoff is 0.0002, the rank-125 cutoff is 0.025, and the
rank-250 cutoff is 0.05. These are comparison thresholds, not separate
requirements for each p-value: BH finds the largest passing rank, then rejects
all hypotheses up to that rank. A prospective look normally has a smaller
spent level than the nominal 0.05.

Repeated prospective looks also need the separate alpha-spending rule described
above; a within-look BH adjustment alone does not cover repeated monitoring.

---

## 11. Inter-rater agreement: do two judges agree?

Switching gears now from "drift detection" to "how do we know our judge LLM is any good."

When we score Verdict's judge against human PASS/FAIL labels, we get pairs like:

- Human said PASS; judge said PASS → agreement
- Human said FAIL; judge said PASS → disagreement
- Human said FAIL; judge said FAIL → agreement

We want a single number that summarizes how often they agree.

### The naive answer: raw agreement %

Just count: out of 100 comparisons, how many times did human and judge agree? Get a percentage. Done.

**Why it's not enough.** Suppose two raters both say PASS 95% of the time.
They can agree often merely because the labels are heavily skewed.

We need a measure that **corrects for chance agreement**.

---

## 12. Gwet's AC1 — chance-corrected agreement

**Gwet's AC1** (Gwet, 2008) was designed to avoid the kappa paradox for nominal, unweighted categories. It uses a different formula for chance agreement that does not become inflated when marginals are skewed.

### The formula

> AC1 = (observed agreement − P_e) / (1 − P_e)
>
> where P_e = Σ [π_c × (1 − π_c)] / (n_categories − 1)
>
> with π_c = (p_a_c + p_b_c) / 2 (average marginal across raters)

The key change: instead of P_e = Σ p_a_c × p_b_c (product of marginals, which inflates on skew), Gwet uses an average-marginal formulation that stays well-behaved.

### Operational interpretation

The `verify_rubric_alignment.py` workflow classifies **pooled AC1 confidence
intervals**, not the point estimate alone:

- 95% CI lower bound ≥ 0.60: `STRONG`.
- Otherwise, 95% CI lower bound ≥ 0.40: `USABLE for drift`.
- Otherwise, 95% CI upper bound < 0.40: `WEAK`.
- Otherwise: `INCONCLUSIVE`; collect more labels.

It reports AC1 beside raw agreement and 95% bootstrap intervals. The
dashboard's separate evaluator-health example-agreement gate uses a Wilson
interval and a different default threshold; it is not an AC1 cutoff.

---

## 13. How it all fits together in Verdict

Two product questions use these methods:

### Flavor 1: "Did this behavior change?"

For each binary metric in an eligible Monitor group:

1. freeze reference and current counts before comparing outcomes;
2. use **Fisher's exact test** for the 2×2 table;
3. report the current-minus-reference rate as the effect size;
4. apply **Benjamini-Hochberg correction** across metrics in that look;
5. for an ongoing monitor, spend the configured alpha across repeated looks with
   the quadratic schedule; and
6. alert only when both the adjusted p-value and minimum absolute rate-change
   thresholds pass.

The `response_empty` metric means the **captured response text** is blank after
trimming whitespace. Calls with no captured response field are excluded from
that metric. A successful tool-only completion can have blank text, so a
significant change in this rate is labeled a text-pattern change, not proof of
a quality regression or task failure.

For every metric cell, the frozen cohort summary also retains the first five
true and first five false unit identities in cohort order. The summaries are the
single stored owner of those identities. For an alert, the dashboard selects
the relevant Boolean state from each cohort so it can open those reference and
current traces without recomputing the comparison after later judgment changes.
They are examples for investigation, not a statistical explanation or
root-cause result.

`UNCLEAR`, missing judgments, and judge errors remain explicit coverage states
outside the PASS/FAIL denominator. Insufficient evidence and stale reference
coverage do not become no-alert results. This is implemented in
`packages/verdict/src/verdict/monitoring.py`.

### Flavor 2: "Is our judge any good?"

Verdict has two distinct checks on human-labeled examples:

- `scripts/verify_rubric_alignment.py` compares human and judge PASS/FAIL
  labels, excludes noncomparable outcomes, and reports raw agreement plus
  **Gwet's AC1 with a 95% bootstrap interval**. Its pooled verdict uses the
  AC1 interval bounds described in §12; raw agreement has no Wilson interval
  in this script.
- The dashboard sentinel workflow reports exact **whole-example agreement**,
  per-label agreement, and a **Wilson interval for whole-example agreement**.
  It requires enough completed examples and no judge errors before declaring
  health; the default threshold applies to the Wilson lower bound. This
  workflow does not calculate AC1.

### The cross-cutting concept

These are different questions with different denominators and gates:

1. **Current Monitor:** compare eligible binary rates with Fisher, BH within
   a look, prospective alpha spending, and a minimum absolute rate difference.
2. **Judge calibration:** compare human and judge labels with agreement and
   uncertainty measures.
3. **Historical diagnostics:** interpret older continuous and distributional
   measures without treating them as current Monitor alert gates.

---

## 14. Quick reference card

| You want to know | Use | Output |
|---|---|---|
| Did two pass rates (binary) differ? | Fisher's exact test | p-value |
| How big is the current binary rate change? | Current-minus-reference rate | −1 to +1; current Monitor uses its absolute value |
| Did two samples (continuous) differ in older analysis? | Mann-Whitney U | p-value; not a current Monitor alert |
| How big was a historical rank-based difference? | Cliff's δ | −1 to +1; not a current Monitor alert gate |
| What standardized mean difference did older analysis report? | Cohen's d | Not a current Monitor alert gate |
| How much mass shifted in older analysis? | Wasserstein distance | ≥ 0; diagnostic |
| Did binned proportions shift in older analysis? | PSI | Heuristic diagnostic, not a p-value |
| I ran many tests — am I just getting false positives? | Benjamini-Hochberg correction | adjusted p-values |
| Did judge evaluability deteriorate? | Deterministic UNCLEAR-rate gate | ≥15-point increase with total-n floor |
| Do two raters agree after chance correction? | Gwet's AC1 | −1 to +1; the rubric-alignment workflow classifies its 95% CI bounds |

The most important point: **p-value and effect size answer different questions and you need both**. p-value alone lets trivial differences trigger alarms with enough data. Effect size alone lets random fluctuations look meaningful. Use them together.

---

## Closing thought

The formulas are not the hard part. The hard part is knowing when to use each
method, what assumptions it makes, and where it breaks. Verdict's methodology is
designed to keep those choices explicit and reproducible.
