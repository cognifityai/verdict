# The Stats Behind Verdict — A Primer for Engineers

A plain-language explanation of the statistical methods behind Monitor and
related diagnostics, with worked examples for engineers.

You should be able to read this in 30-40 minutes and come away understanding
the statistics behind Monitor, semantic drift, and judge calibration.

---

## Contents

1. [The fundamental question we keep asking](#1-the-fundamental-question-we-keep-asking)
2. [Distributions, samples, and how to compare them](#2-distributions-samples-and-how-to-compare-them)
3. [The significance test: Fisher's exact & Mann-Whitney U — does this group differ from that group?](#3-the-significance-test-fishers-exact-binary-and-mann-whitney-u-continuous)
4. [p-values — and what they actually mean](#4-p-values--and-what-they-actually-mean)
5. [Effect sizes — "yes, but how much?"](#5-effect-sizes--yes-but-how-much)
6. [Cliff's δ — ranked-data effect size](#6-cliffs-δ--ranked-data-effect-size)
7. [Cohen's d — continuous-score background](#7-cohens-d--continuous-score-background)
8. [Wasserstein distance — Earth Mover's Distance](#8-wasserstein-distance--earth-movers-distance)
9. [Population Stability Index — PSI](#9-population-stability-index--psi)
10. [Multi-testing and Benjamini-Hochberg correction](#10-multi-testing-and-benjamini-hochberg-correction)
11. [Inter-rater agreement: do two judges agree?](#11-inter-rater-agreement-do-two-judges-agree)
12. [Gwet's AC1 — chance-corrected agreement](#12-gwets-ac1--chance-corrected-agreement)
13. [How it all fits together in Verdict](#13-how-it-all-fits-together-in-verdict)
14. [Quick reference card](#14-quick-reference-card)

---

## 1. The fundamental question we keep asking

Everything Verdict does is some version of one question:

> *"Did something change?"*

And by "change," we usually mean: *"is the quality of LLM responses today actually different from the quality yesterday, or am I just seeing random noise?"*

There's no magic involved. You collect a sample of yesterday's responses, you collect a sample of today's responses, and then you compare them. The hard part is the comparing: when you only have a sample, not the whole universe, how do you know whether the differences you see are real or just random fluctuation?

That's the question statistics answers. The rest of this primer is just the specific tools that answer different versions of it.

---

## 2. Distributions, samples, and how to compare them

Before we can ask "did it change," we need a way to talk about "it."

**A distribution** is the answer to: "if I look at a bunch of LLM responses on a particular task, how often does each quality level show up?" It's a histogram, conceptually. For our PASS/FAIL judge, the distribution might be:

- 90% PASS, 10% FAIL → "this model is usually good on this task"
- 50% PASS, 50% FAIL → "this model is unreliable on this task"
- 100% PASS, 0% FAIL → "this model is perfect on this task" (suspicious)

The distribution captures the underlying tendency of the system. A response is a single sample from that distribution.

**The catch**: you never see the true distribution. You only see samples. Maybe the model is *truly* 80% PASS, but the 17 responses you sampled yesterday happened to be 15 PASS and 2 FAIL (88%). Today you sample 18 responses and get 11 PASS, 7 FAIL (61%). Did the underlying distribution change? Or did you just get unlucky today?

That's what every statistical test we use is designed to answer.

### The two-distribution comparison

Concretely, a Monitor comparison has:

- **Baseline window** — the historical samples (e.g. 17 judgments from yesterday)
- **Current window** — the recent samples (e.g. 18 judgments from today)

We ask: "are these two windows samples from the same underlying distribution, or different ones?"

Monitor uses Fisher's exact test for binary PASS/FAIL data. Mann-Whitney U is
an example of a test for ranked or continuous data, not a Monitor alert gate.

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
coverage evidence, not pooled observations.

The **Logical session (descriptive)** preview is intentionally outside this
inferential procedure. It groups native Agent runs only by explicit
`AgentRun.session_id` and reports reference/current rates, raw effects, and
coverage as of the preview. Verdict does not yet observe authoritative session
finalization or establish independence between those units, so it computes no
Fisher test, adjusted p-value, alert decision, or prospective activation for
that view.

---

## 3. The significance test: Fisher's exact (binary) and Mann-Whitney U (continuous)

The most common way to compare two samples is the **t-test**, which assumes the underlying distributions are roughly normal (bell-curve shaped). Our PASS/FAIL data is **not** normal at all — it's binary (0 or 1) — so a t-test is technically inappropriate.

We pick the significance test by data type:

- **Binary PASS/FAIL dimensions (the common case)** → **Fisher's exact test**. Comparing two pass rates is really comparing two proportions, and the question "did the pass rate change?" is a 2×2 table: (pass vs fail) × (current vs baseline). Fisher's exact computes the probability of seeing a split this lopsided (or more) if the true rate were unchanged — *exactly*, with no large-sample approximation. That makes it the textbook test for this comparison and well-behaved at the small per-window sample sizes drift detection often runs at.
- **Ordinal / continuous scores** → **Mann-Whitney U** is one possible
  non-parametric tool, depending on the question and sampling assumptions.

The rest of this section explains Mann-Whitney U as background. Monitor's
binary comparisons use Fisher's exact test.

**Mann-Whitney U** (also called the Wilcoxon rank-sum test) is the *non-parametric* alternative to the t-test. It makes no assumption about the shape of the underlying distribution. It works on the order of the values, not their specific magnitudes.

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

- Our data is binary PASS/FAIL → not normal → t-test invalid.
- For binary outcomes, Mann-Whitney degrades into a heavily-tied rank test that's only a weaker proxy for a two-proportion comparison — so we use **Fisher's exact test**, which answers exactly that question without approximation. Verdict's dependency-free implementation computes the two-sided p-value from the 2×2 (pass/fail × current/baseline) table and is differentially checked against SciPy.
- For ordinal or continuous scores, Mann-Whitney can compare ranks when its
  sampling assumptions fit the question. Monitor does not score these values.
- Monitor pairs Fisher's exact test with a **pass-rate difference** as its
  effect size. Cliff's δ is explained below as background.

---

## 4. p-values — and what they actually mean

The p-value is the single most-misunderstood statistic in all of science. Let's nail it down.

The p-value answers a *specific* question:

> *"If the two samples came from the same underlying distribution — if the apparent difference were pure random noise — what's the probability of seeing data this extreme or MORE extreme just by chance?"*

A p-value of 0.005 means: "If there were no real difference, there's only a 0.5% chance we'd see this pattern. So either we got incredibly unlucky, or there's actually a difference."

By convention, when p < 0.05 (5%), we say "the result is statistically significant" and conclude there's probably a real difference.

### The intuition pump

Flip a coin 100 times. You get 60 heads. Is the coin biased?

- Under "the coin is fair" (the *null hypothesis*), the chance of getting 60+ heads in 100 flips is about 2.8%.
- That's small. So we reject "the coin is fair" and conclude it's probably biased.
- The 2.8% is the p-value.

If you got 52 heads instead, the p-value would be about 0.69 — totally consistent with a fair coin, you can't reject it.

### What p-value is NOT

The single most common mistake: **p-value is not "the probability the null hypothesis is true."**

- A p-value of 0.05 does NOT mean "there's a 5% chance the samples come from the same distribution."
- It means "IF the samples came from the same distribution, there'd be a 5% chance of seeing data this extreme."

This is the difference between P(data | null) and P(null | data). Different things. Bayesian methods give you the latter; frequentist p-values give you the former. We use frequentist methods, so when we say "p < 0.05" we mean the data is unlikely under the null, NOT that the null is unlikely given the data.

### The threshold

The "p < 0.05 = significant" cutoff comes from R. A. Fisher's 1925 textbook. It's a convention, not a law of nature. More-conservative thresholds (p < 0.01, p < 0.001) are appropriate for higher-stakes decisions.

Monitor's default nominal threshold is **0.05**. Historical previews use that
threshold. An active monitor spends it across repeated comparisons, so later
looks have smaller thresholds.

### What our drift detector does with p-values

For each eligible Monitor group and binary metric, Verdict uses Fisher's exact
test to get a p-value. It then:

1. Adjust the p-value for multiple comparisons (see §10 below)
2. Compare the adjusted p-value with that comparison's threshold
3. If it passes AND the absolute rate difference is large enough, emit an alert

p-value alone isn't enough — it only tells you "this isn't noise." It doesn't tell you HOW MUCH things changed. That's what effect sizes are for.

---

## 5. Effect sizes — "yes, but how much?"

Here's the dirty secret of p-values: with enough data, you can detect *any* difference as "significant," no matter how trivially small.

Imagine two distributions where the means differ by 0.001. With a million samples, p-value will be tiny, and you'd reject the null and say "they're different!" But practically, who cares — 0.001 is irrelevant.

That's where **effect size** comes in. An effect size answers:

> *"How big is the difference, in practical terms?"*

p-value says: "is it noise?" → yes/no.
Effect size says: "how big is the signal?" → magnitude.

You need BOTH. p-value without effect size lets trivial differences trigger alarms. Effect size without p-value lets random fluctuations look meaningful.

Monitor's default alert gate requires:
- **BH-adjusted p ≤ the comparison's threshold** AND
- **absolute pass-rate difference ≥ 0.10**

Both gates have to pass for a signal to fire.

There are many different effect sizes — Cohen's d, Cliff's δ, Glass's Δ, Hedges' g, the list goes on. They differ in what they assume about the data and how they're scaled. Let's look at the two we care about.

---

## 6. Cliff's δ — ranked-data effect size

**Cliff's δ** (Cliff, 1996) is a non-parametric effect size. It pairs correctly with Mann-Whitney U because both make no assumption about the underlying distribution.

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

### Why Cliff's δ can help with ranked data

- **Pairs with Mann-Whitney U.** Both are non-parametric. Same family.
- **Bounded in [−1, +1].** Easy to interpret. Easy to threshold.
- **Robust to outliers and weird distributions.** Doesn't break on binary data.
- **Directly interpretable.** "The probability your post-regression response is worse than a pre-regression response is 39 percentage points higher than the reverse."

Monitor's binary alert gate uses a current-minus-reference rate difference,
not Cliff's δ.

---

## 7. Cohen's d — continuous-score background

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

Current Monitor comparisons use a directly interpretable rate difference for
binary metrics. Cohen's d is not a Monitor alert gate.

### When Cohen's d would be appropriate

For continuous quality scores (e.g., a judge that returns a 0-100 number rather
than PASS/FAIL), Cohen's d can be useful when its assumptions hold.

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

### Where it is useful

- **More sensitive to small persistent shifts** than Mann-Whitney, especially on continuous data.
- **Directly interpretable.** "About 39% of mass moved from 1 to 0."
- **Symmetric and metric.** It's a real distance function in mathematical terms; satisfies triangle inequality.
- **Common in ML monitoring.** Useful as a secondary distance-based view of
  distribution movement.

We compute it via `scipy.stats.wasserstein_distance` (a few lines of code, no new dependency).

### Limits

- For binary data, it's nearly redundant with Cliff's δ (they capture the same shift).
- More valuable on continuous quality scores.
- Verdict's structural analyzer uses it for length and latency checks, and the
  semantic analyzer reports it as a diagnostic. Monitor's binary alert gate
  does not use it.

---

## 9. Population Stability Index — PSI

PSI describes how much a categorical or binned distribution has shifted.
Verdict's semantic analyzer reports it as a diagnostic; Monitor's binary alert
gate does not use it.

### The formula

For each bin, compute:

> Contribution = (current % − baseline %) × ln(current % / baseline %)

Then sum across all bins. The result is PSI.

### Interpretation

Higher PSI indicates a larger binned shift. Verdict's semantic analyzer computes
this diagnostic on one embedding axis using quantile bins derived from the
reference sample. It returns zero when either sample has fewer than two values
or the reference values cannot form at least two bins. PSI alone does not cause
a Monitor alert.

---

## 10. Multi-testing and Benjamini-Hochberg correction

Here's a subtle problem that bites every statistics-naïve product.

**The problem.** If you run 100 statistical tests at p < 0.05, you'd expect ~5 to come up "significant" purely by chance. So if your dashboard runs 50 (cluster × dimension) tests every hour, you'd expect ~2.5 false-positive drift alerts per hour even when literally nothing has changed.

This is the **multiple comparisons problem**.

### The Bonferroni approach (too conservative)

The classic fix: divide your significance threshold by the number of tests. If you're running 100 tests and want overall false-positive rate < 5%, require each individual test to pass at p < 0.0005 (= 0.05/100). This is **Bonferroni correction**.

Problem: Bonferroni is extremely conservative. With 100 tests, it nearly eliminates your ability to detect anything real. Most real drift signals will get missed.

### The Benjamini-Hochberg approach (the standard)

**Benjamini-Hochberg** (1995) controls a different quantity: **False Discovery Rate (FDR)**. Instead of guaranteeing "almost no false positives" (Bonferroni), it controls "what fraction of the alerts we emit will be false positives" — typically targeted at 5% or 1%.

The procedure:

1. Rank all your p-values from smallest to largest.
2. For each p-value at rank k out of N, compute its adjusted threshold: `(k/N) × α`.
3. The smallest p-value needs to be very small; the largest only needs to be below α.
4. Find the largest k where p_k ≤ (k/N) × α. Everything at rank ≤ k is "significant."

### Why BH is the modern standard

- Much less conservative than Bonferroni.
- Controls a meaningful quantity (proportion of false positives in your "discoveries").
- Used in essentially every modern eval/ML paper.
- 13 lines of Python to implement.

### Our application

When Monitor compares N eligible group/metric pairs, it applies BH to those N
p-values together. The declared family is one reviewed comparison: every
scored hypothesis that could alert in that look.

Monitor's current scored path uses binary metrics and Fisher's exact test.

For a comparison with 250 eligible cells at a 0.05 threshold, the first ranked
p-value must be at most 0.0002 to pass on its own. If many cells have small
p-values, BH can admit more than the first. An active monitor also applies its
smaller sequential threshold for that look.

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

Verdict currently uses the same operational thresholds for AC1:

- AC1 ≥ 0.80: strong
- AC1 0.60–0.80: acceptable
- AC1 0.40–0.60: preliminary
- AC1 < 0.40: unreliable

Verdict reports AC1 beside raw agreement and confidence intervals so the point
estimate is never read without its sample uncertainty.

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

For customer-labeled PASS/FAIL examples:

1. Run the configured binary rubric judge.
2. Keep `UNCLEAR` and execution errors outside the comparable denominator.
3. Report raw exact-match agreement with a Wilson interval.
4. Report **Gwet's AC1** as the chance-corrected agreement measure.
5. Use a bootstrap confidence interval rather than trusting the point estimate.

This is `scripts/verify_rubric_alignment.py` and the dashboard calibration flow.

### The cross-cutting concept

Verdict uses separate methods for separate questions. Monitor gates binary alerts
with Fisher's exact test, a rate difference, and BH correction. Structural and
semantic analyzers assess distribution changes separately. Judge calibration measures
agreement with independent labels. A diagnostic by itself does not become a
Monitor alert.

---

## 14. Quick reference card

| You want to know | Use | Output |
|---|---|---|
| Did two pass rates (binary) differ? | Fisher's exact test | p-value |
| Did two samples (continuous) come from the same distribution? | Mann-Whitney U | p-value |
| How big is the difference (non-parametric)? | Cliff's δ | -1 to +1 |
| How big is the difference (assumes normal)? | Cohen's d | -∞ to +∞ |
| How much mass shifted from one distribution to the other? | Wasserstein distance | ≥ 0 |
| How large is a binned distribution shift? | PSI | ≥ 0; interpret with the sample and binning method |
| I ran many tests — am I just getting false positives? | Benjamini-Hochberg correction | adjusted p-values |
| Did judge evaluability deteriorate? | Deterministic UNCLEAR-rate gate | ≥15-point increase with total-n floor |
| Do two raters agree after chance correction? | Gwet's AC1 | -1 to +1; ≥ 0.6 acceptable |

The most important point: **p-value and effect size answer different questions and you need both**. p-value alone lets trivial differences trigger alarms with enough data. Effect size alone lets random fluctuations look meaningful. Use them together.

In the table above, only Monitor's binary comparison methods directly gate
Monitor alerts; the other methods provide diagnostics or background.

---

## Closing thought

The formulas are not the hard part. The hard part is knowing when to use each
method, what assumptions it makes, and where it breaks. Verdict's methodology is
designed to keep those choices explicit and reproducible.
