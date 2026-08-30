# The Stats Behind Verdict — A Primer for Engineers

A plain-language explanation of every statistical method Verdict uses — what it is, why it was chosen over the alternatives, and how to interpret its output. Written for engineers who are strong at code but didn't take graduate statistics: it starts from intuition and builds up, with worked examples.

You should be able to read this in 30-40 minutes and come away genuinely understanding what's happening inside the drift detector and the judge-alignment scripts. No black boxes by the end.

---

## Contents

1. [The fundamental question we keep asking](#1-the-fundamental-question-we-keep-asking)
2. [Distributions, samples, and how to compare them](#2-distributions-samples-and-how-to-compare-them)
3. [The significance test: Fisher's exact & Mann-Whitney U — does this group differ from that group?](#3-the-significance-test-fishers-exact-binary-and-mann-whitney-u-continuous)
4. [p-values — and what they actually mean](#4-p-values--and-what-they-actually-mean)
5. [Effect sizes — "yes, but how much?"](#5-effect-sizes--yes-but-how-much)
6. [Cliff's δ — the right effect size for us](#6-cliffs-δ--the-right-effect-size-for-us)
7. [Cohen's d — kept for legacy reasons](#7-cohens-d--kept-for-legacy-reasons)
8. [Wasserstein distance — Earth Mover's Distance](#8-wasserstein-distance--earth-movers-distance)
9. [Population Stability Index — PSI](#9-population-stability-index--psi)
10. [Multi-testing and Benjamini-Hochberg correction](#10-multi-testing-and-benjamini-hochberg-correction)
11. [Inter-rater agreement: do two judges agree?](#11-inter-rater-agreement-do-two-judges-agree)
12. [Cohen's κ and its paradox](#12-cohens-κ-and-its-paradox)
13. [Gwet's AC2 — the paradox fix](#13-gwets-ac2--the-paradox-fix)
14. [Bradley-Terry — turning pairwise wins into rankings](#14-bradley-terry--turning-pairwise-wins-into-rankings)
15. [How it all fits together in Verdict](#15-how-it-all-fits-together-in-verdict)
16. [Quick reference card](#16-quick-reference-card)

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

Concretely, our drift detector always has:

- **Baseline window** — the historical samples (e.g. 17 judgments from yesterday)
- **Current window** — the recent samples (e.g. 18 judgments from today)

We ask: "are these two windows samples from the same underlying distribution, or different ones?"

That's what a two-sample test answers — Fisher's exact for binary PASS/FAIL data, Mann-Whitney U for continuous metrics.

**Implementation note:** both repository workflows use the associated captured
trace's `started_at` timestamp, not the later time when a judge scored it. The
legacy `verdict-pipeline` defaults to a 24-hour current window and a 7-day
baseline ending 24 hours before analysis. `verdict-local` and
`verdict-monitor` use equal, non-overlapping older/newer session-count cohorts.

---

## 3. The significance test: Fisher's exact (binary) and Mann-Whitney U (continuous)

The most common way to compare two samples is the **t-test**, which assumes the underlying distributions are roughly normal (bell-curve shaped). Our PASS/FAIL data is **not** normal at all — it's binary (0 or 1) — so a t-test is technically inappropriate.

We pick the significance test by data type:

- **Binary PASS/FAIL dimensions (the common case)** → **Fisher's exact test**. Comparing two pass rates is really comparing two proportions, and the question "did the pass rate change?" is a 2×2 table: (pass vs fail) × (current vs baseline). Fisher's exact computes the probability of seeing a split this lopsided (or more) if the true rate were unchanged — *exactly*, with no large-sample approximation. That makes it the textbook test for this comparison and well-behaved at the small per-window sample sizes drift detection often runs at.
- **Ordinal / continuous scores** → **Mann-Whitney U** (below). If you ever score on a scale rather than PASS/FAIL, this is the right non-parametric tool.

The rest of this section explains Mann-Whitney U, which is the more general of the two; Fisher's exact is the specialization we use when the scores are binary.

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
- For binary outcomes, Mann-Whitney degrades into a heavily-tied rank test that's only a weaker proxy for a two-proportion comparison — so we use **Fisher's exact test**, which answers exactly that question without approximation. `scipy.stats.fisher_exact` on the 2×2 (pass/fail × current/baseline) table returns the p-value directly.
- For ordinal/continuous scores there are no such ties, and Mann-Whitney is exactly the right non-parametric two-sample test — the one LMSys's Chatbot Arena, Arena-Hard-Auto, and most modern LLM eval work reach for.
- Either way we stay non-parametric and pair the test with **Cliff's δ** as the effect size (next section).

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

In Verdict, we default to **p < 0.01** for emitting drift alerts because we'd rather be slow to alarm than spammy.

### What our drift detector does with p-values

For each (cluster, dimension) pair, we run the significance test (Fisher's exact for binary PASS/FAIL, Mann-Whitney U for continuous) and get a p-value. We then:

1. Adjust the p-value for multiple comparisons (see §10 below)
2. Compare against our threshold (e.g., 0.01)
3. If it passes AND the effect size is large enough (see §5–7), emit a drift signal

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

Verdict's drift detector requires:
- **BH-adjusted p < 0.01** AND
- **|Cliff's δ| > 0.147** (the "small effect" threshold)

Both gates have to pass for a signal to fire.

There are many different effect sizes — Cohen's d, Cliff's δ, Glass's Δ, Hedges' g, the list goes on. They differ in what they assume about the data and how they're scaled. Let's look at the two we care about.

---

## 6. Cliff's δ — the right effect size for us

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

### Why Cliff's δ is right for us

- **Pairs with Mann-Whitney U.** Both are non-parametric. Same family.
- **Bounded in [−1, +1].** Easy to interpret. Easy to threshold.
- **Robust to outliers and weird distributions.** Doesn't break on binary data.
- **Directly interpretable.** "The probability your post-regression response is worse than a pre-regression response is 39 percentage points higher than the reverse."

This is what the current drift detector uses for effect-size gating.

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

That's why we report Cohen's d in our DriftSignal output but **gate on Cliff's δ instead**. Cohen's d is reported for readers familiar with the d scale; Cliff's δ is what we use for decisions.

### When Cohen's d would be appropriate

For continuous quality scores (e.g., a judge that returns a 0-100 number rather than PASS/FAIL), Cohen's d would be fine. The standard deviation makes sense for continuous data. We'll likely add continuous-score judges in v1, and at that point Cohen's d becomes valid alongside Cliff's δ.

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

### Why we use it alongside Mann-Whitney

- **More sensitive to small persistent shifts** than Mann-Whitney, especially on continuous data.
- **Directly interpretable.** "About 39% of mass moved from 1 to 0."
- **Symmetric and metric.** It's a real distance function in mathematical terms; satisfies triangle inequality.
- **Common in ML monitoring.** Useful as a secondary distance-based view of
  distribution movement.

We compute it via `scipy.stats.wasserstein_distance` (a few lines of code, no new dependency).

### Limits

- For binary data, it's nearly redundant with Cliff's δ (they capture the same shift).
- More valuable on continuous quality scores.
- Still report it as a secondary confirmatory signal.

---

## 9. Population Stability Index — PSI

PSI is the third drift signal we compute. It's standard in credit risk modeling and increasingly common in ML observability. It tells you how much a categorical or binned distribution has shifted.

### The formula

For each bin, compute:

> Contribution = (current % − baseline %) × ln(current % / baseline %)

Then sum across all bins. The result is PSI.

### Interpretation

Industry-standard thresholds:

- **PSI < 0.1**: no significant population change
- **0.1 ≤ PSI < 0.25**: moderate population change — investigate
- **PSI ≥ 0.25**: significant population change — investigate immediately

### Why we use it

- Industry-standard. Auditors and ML platform teams expect to see PSI numbers.
- Captures distributional shift on binned data.
- Easy to compute and interpret.

### Discrete and constant data

The implementation uses one category bin per distinct value when there are few
unique values, including binary PASS/FAIL. This avoids empty linear bins and
still detects a shift from an all-PASS baseline to a mixed current window. If
both windows contain the same single constant value, PSI is correctly zero. For
continuous data it uses baseline-driven linear edges; a constant continuous
baseline remains a limitation, so read PSI alongside Wasserstein and the
primary Fisher/Cliff gates rather than as an independent alert.

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

When we run drift detection across N eligible scored (cluster, dimension) pairs,
we apply BH to those N p-values together. The declared family is one call to
`DriftDetector.detect()`: every scored hypothesis that could emit an alert in
that run. Deterministic `UNCLEAR`-rate alerts have no p-value and are outside the
BH family.

If a future caller mixes binary and continuous windows, Fisher's-exact and
Mann-Whitney p-values remain in that same family. They test different data types,
but each is a valid null p-value for a simultaneously alertable hypothesis.
Splitting the family by test implementation would make the correction change as
data types change and would no longer match the product question, "which cells
alerted in this run?" Today the production judge emits binary PASS/FAIL scores,
so its scored drift path uses Fisher's exact throughout and the mixed-test
distinction is normally inactive.

For a customer with 50 intent clusters × 5 dimensions = 250 tests:
- Smallest p-value needs to be < 0.0002 to pass BH at α=0.05
- Median needs to be < 0.01
- Largest only needs to be < 0.05

That keeps us honest. Without BH correction, customers would mute our alerts within a day.

---

## 11. Inter-rater agreement: do two judges agree?

Switching gears now from "drift detection" to "how do we know our judge LLM is any good."

When we score Verdict's judge against MT-Bench's real human judgments, we get pairs like:

- Human said "A wins"; judge said "A wins" → agreement
- Human said "A wins"; judge said "B wins" → disagreement
- Human said "tie"; judge said "A wins" → disagreement

We want a single number that summarizes how often they agree.

### The naive answer: raw agreement %

Just count: out of 100 comparisons, how many times did human and judge agree? Get a percentage. Done.

**Why it's not enough.** Suppose I have two raters who both say "A wins" 95% of the time. They'll agree about 90% of the time just by both being biased toward A — even if their decisions are independent. Raw agreement of 90% sounds great but mostly reflects shared bias, not real skill.

We need a measure that **corrects for chance agreement**.

---

## 12. Cohen's κ and its paradox

**Cohen's kappa** (1960) is the most famous chance-corrected agreement measure.

### The formula

> κ = (observed agreement − chance agreement) / (1 − chance agreement)

Where "chance agreement" is what you'd expect if both raters were independent given their marginal distributions.

### Interpretation (Landis & Koch, 1977)

- κ ≥ 0.80: strong
- κ 0.60–0.80: acceptable
- κ 0.40–0.60: preliminary / borderline
- κ < 0.40: unreliable

### Why κ can look harsher than raw agreement

In judge-alignment sweeps with skewed labels, Cohen's κ can look much worse than
raw agreement. That does not mean κ should be ignored; it means the marginal
distributions matter, and you need to understand what the statistic is
correcting for before treating it as the whole story.

### The Kappa Paradox

But here's the subtle issue: **Cohen's κ has a known mathematical problem called the "kappa paradox."**

When both raters agree on most cases (high overall agreement) BUT the marginal distribution is skewed (e.g., both rate 95% PASS), the chance-agreement term in the denominator gets very large. The (1 − chance agreement) gets small. The whole fraction gets squashed toward zero — even when raw agreement is high.

In plain English: when judges agree on the easy cases (most things are PASS) and only disagree on the hard cases, Cohen's κ can be artificially deflated, making good agreement look bad.

**This is exactly our situation.** Our judges all rate ~90% of responses PASS. Most pairs have an obvious winner that both judge and human agree on. The disagreements are clustered in genuinely-close pairs. Cohen's κ punishes this even though it's the natural pattern.

The paradox has been known since Feinstein and Cicchetti (1990) and is one of the main reasons modern methodology papers prefer alternatives.

---

## 13. Gwet's AC2 — the paradox fix

**Gwet's AC2** (Gwet, 2008) was specifically designed to fix the kappa paradox. It uses a different formula for chance agreement that doesn't get inflated when marginals are skewed.

### The formula

> AC2 = (observed agreement − P_e) / (1 − P_e)
>
> where P_e = Σ [π_c × (1 − π_c)] / (n_categories − 1)
>
> with π_c = (p_a_c + p_b_c) / 2 (average marginal across raters)

The key change: instead of P_e = Σ p_a_c × p_b_c (product of marginals, which inflates on skew), Gwet uses an average-marginal formulation that stays well-behaved.

### Same interpretation scale

You can use the same Landis & Koch thresholds for AC2:

- AC2 ≥ 0.80: strong
- AC2 0.60–0.80: acceptable
- AC2 0.40–0.60: preliminary
- AC2 < 0.40: unreliable

The difference is *what number you get*, not how to interpret it.

### Worked example showing the paradox

Suppose two raters score 100 items and agree on 99:

| Rater A | Rater B | Count |
|---|---|---|
| PASS | PASS | 94 |
| PASS | FAIL | 1 |
| FAIL | PASS | 0 |
| FAIL | FAIL | 5 |

Raw agreement is 99/100 = 99%. Clearly the raters agree.

- Cohen's κ ≈ 0.85 (still high, but starting to feel the paradox)

Now consider an extreme case: 99 PASSes both, 1 disagreement.

| Rater A | Rater B | Count |
|---|---|---|
| PASS | PASS | 95 |
| PASS | FAIL | 1 |
| FAIL | PASS | 4 |
| FAIL | FAIL | 0 |

Raw agreement is still 95/100 = 95%.

- Cohen's κ ≈ -0.04 (catastrophically dropped to near zero, paradoxically)
- Gwet's AC2 ≈ 0.85 (handles the skew correctly)

Same raters, same raw agreement, but Cohen's κ drops to nothing while AC2 stays sensible.

### Why Verdict reports both

Verdict reports **both** Cohen's κ and Gwet's AC2 side by side with a
methodology note. Readers familiar with κ get their reference number, while
readers who know about the paradox get the methodologically safer statistic for
skewed rubric data.

For user-signal correlation, Verdict computes the binary confusion matrix only
from one unambiguous PASS/FAIL plus positive/negative observation per trace.
`UNCLEAR` and non-label signals are coverage skips, exact duplicates collapse,
and contradictory usable duplicates are excluded and counted. Raw agreement
gets a Wilson interval; Cohen's κ and Gwet's coefficient get deterministic
bootstrap intervals. A leniency rate means “user-negative among responses the
judge called PASS,” while a strictness rate means “user-positive among responses
the judge called FAIL,” so each uses the judge decision it conditions on as its
denominator.

---

## 14. Bradley-Terry — turning pairwise wins into rankings

Last big concept. This one's about ranking LLMs against each other.

### The problem

You want to rank N models (Claude, GPT, Gemini, Llama, ...) by quality on your traffic. You can only test them pairwise — each match-up compares two models on the same prompt. How do you turn a pile of pairwise outcomes into a single ranking with confidence intervals?

Same problem as ranking chess players (each game is pairwise), tennis players, or any tournament. The classic solution is **Bradley-Terry**.

### The model

Each model has a hidden "strength" parameter β:

> P(A beats B) = e^(β_A) / (e^(β_A) + e^(β_B))

If β_A = β_B, P = 0.5 (coin flip).
If β_A >> β_B, P → 1 (A almost always wins).

This is just a logistic function applied to the difference in strengths.

### How to fit it

Given a bunch of pairwise outcomes, find the β values that maximize the likelihood of those observations. **This is just logistic regression with a special feature design**:

- One row per pairwise game.
- Features: a vector of zeros except +1 in position of model A, −1 in position of model B.
- Label: 1 if A won, 0 if B won, 0.5 if tie.

That last label is valid only when the judge explicitly returned a usable tie.
Malformed or missing verdict markers and provider failures are not games and
are not coded as `0.5`. Verdict excludes them from the fit, reports them as
coverage loss, and rejects unknown winner values before constructing the design
matrix. Otherwise an outage could pull two models artificially toward equal
ratings.

Hand this to scikit-learn's `LogisticRegression(fit_intercept=False)` and you get back β coefficients. Those are the model strengths.

### Confidence intervals via bootstrap

A single point estimate of β isn't enough — you also need to know how confident you are. **Bootstrap**: resample your pairwise outcomes with replacement, fit BT each time, repeat N=1000 times. The 2.5th and 97.5th percentiles of each model's β give you a 95% confidence interval.

### Translating to actionable numbers

The β values themselves are abstract. Two more useful translations:

- **Win rate vs. anchor**: pick one model as the reference (typically the customer's current production model). For each other model, P(other beats anchor) = 1 / (1 + e^(β_anchor − β_other)). This directly answers "would I be better off switching?"
- **Per-cluster ranking**: fit BT separately for each intent cluster. The same model can win on coding-question traffic and lose on writing-question traffic. Per-cluster rankings surface this.

### Where you've seen it

LMSys's Chatbot Arena leaderboard. Every time someone votes "A is better" on https://lmarena.ai, that's a Bradley-Terry input. The headline numbers on the leaderboard are BT ratings with bootstrap confidence intervals.

We use the same code structure (FastChat's `compute_elo.py`), except our voters are LLM judges instead of humans, and rankings are computed on the workload being evaluated.

---

## 15. How it all fits together in Verdict

Three flavors of question, three pipelines:

### Flavor 1: "Did this model drift?"

For each (cluster, dimension):

1. **Fisher's exact test** (binary dimensions) / **Mann-Whitney U** (continuous) → p-value: is the current window distributionally different from the baseline?
2. **Cliff's δ** → effect size: how big is the difference in non-parametric terms?
3. **Wasserstein distance** → second-opinion effect size (on binary data this equals the pass-rate difference)
4. **PSI** → distributional drift metric (binned by category for binary/discrete data)
5. **Benjamini-Hochberg correction** across all simultaneous tests → adjusted p-values
6. **Emit a DriftSignal** when BH-adjusted p < 0.01 AND |Cliff's δ| > 0.147

Separately, Verdict tracks evaluability. `UNCLEAR` is excluded from PASS/FAIL,
but an alert is emitted when the current UNCLEAR fraction rises by at least 15
percentage points and both windows meet the total-sample floor. This is a
deterministic coverage rule, not a p-value test, so the signal reports p-value
and Cliff's δ as not applicable and stays outside the BH family.

This is `packages/verdict_eval/src/verdict_eval/drift.py`.

### Flavor 2: "Is our judge any good?"

For a sample of MT-Bench pairs (or a customer's labeled subset):

1. Run our pairwise judge with position-swap on each pair.
2. Exclude invalid/error outputs, report their pair and component coverage, and
   fail the evidence gate if the selected run is incomplete.
3. Compare the remaining usable verdicts to the human ground truth.
4. **Cohen's κ** → chance-corrected agreement (paradox-vulnerable on skewed marginals)
5. **Gwet's AC2** → chance-corrected agreement (paradox-corrected; preferred for our data)
6. **Non-tie agreement** → raw agreement on cases where humans had a clear winner

Both κ and AC2 are reported side by side. Workload-specific calibration should
determine whether rankings are shown as decision support or treated as
review-only.

This is `scripts/verify_judge_alignment.py`.

### Flavor 3: "Which LLM is best for my traffic?"

For a customer who shadow-routes some traffic to multiple providers:

1. For each shadow pair, the **pairwise judge** with **position-swap consistency** returns a usable winner/tie or an explicit invalid/error state.
2. Across all pairs, fit **Bradley-Terry** logistic regression with **bootstrap confidence intervals**.
3. Report per-cluster rankings + win-rate vs. anchor model.

Only usable outcomes enter step 2. An invalid/error outcome must stop or fail the
owning workflow's coverage gate; silently turning it into a tie changes the
measurement rather than handling the failure.

This is `packages/verdict_eval/src/verdict_eval/compare.py` plus `pairwise.py`.

### The cross-cutting concept

Notice that every pipeline does the same three things in different ways:

1. **Compare two distributions** (Fisher/Mann-Whitney, Cliff's δ, Wasserstein)
2. **Quantify the disagreement** between observations (κ, AC2, BT win rate)
3. **Correct for chance / multiple testing** (BH adjustment, chance-corrected agreement)

That's basically all of frequentist statistics in three sentences.

---

## 16. Quick reference card

| You want to know | Use | Output |
|---|---|---|
| Did two pass rates (binary) differ? | Fisher's exact test | p-value |
| Did two samples (continuous) come from the same distribution? | Mann-Whitney U | p-value |
| How big is the difference (non-parametric)? | Cliff's δ | -1 to +1 |
| How big is the difference (assumes normal)? | Cohen's d | -∞ to +∞ |
| How much mass shifted from one distribution to the other? | Wasserstein distance | ≥ 0 |
| Is the distributional shift industry-significant? | PSI | < 0.1 stable, ≥ 0.25 shifted |
| I ran many tests — am I just getting false positives? | Benjamini-Hochberg correction | adjusted p-values |
| Did judge evaluability deteriorate? | Deterministic UNCLEAR-rate gate | ≥15-point increase with total-n floor |
| Do two raters agree (chance-corrected)? | Cohen's κ | -1 to +1; ≥ 0.6 acceptable |
| Same but doesn't break on skewed marginals? | Gwet's AC2 | same scale |
| Turn pairwise wins into a ranking with CIs? | Bradley-Terry + bootstrap | per-model rating + CI |

The most important point: **p-value and effect size answer different questions and you need both**. p-value alone lets trivial differences trigger alarms with enough data. Effect size alone lets random fluctuations look meaningful. Use them together.

---

## Closing thought

The formulas are not the hard part. The hard part is knowing when to use each
method, what assumptions it makes, and where it breaks. Verdict's methodology is
designed to keep those choices explicit and reproducible.
