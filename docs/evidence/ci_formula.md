# Confidence Interval Formula and Covariance Sign Analysis

## 1. Verbatim Implementation in analysis_decider.py

In `/home/ubuntu/refine-exp/preregistration/analysis_decider.py` (lines 145-150):

```python
def paired_rd_ci(n11, n10, n01, n00, N, z=1.96):
    """Paired risk difference RD=(n10-n01)/N and Agresti Wald 95% CI."""
    rd = (n10 - n01) / N
    var = ((n11+n10)*(n01+n00) + (n11+n01)*(n10+n00) - 2*(n01*n10 - n11*n00)) / (N**3)
    se = math.sqrt(max(var, 0.0))
    return rd, se, rd - z*se, rd + z*se
```

## 2. Standard Formula for Paired Proportions

For paired binary data with contingency table counts $n_{11}, n_{10}, n_{01}, n_{00}$ where $N = n_{11} + n_{10} + n_{01} + n_{00}$:
* $p_1 = \frac{n_{11} + n_{10}}{N}$
* $p_2 = \frac{n_{11} + n_{01}}{N}$
* Risk Difference: $\text{RD} = p_1 - p_2 = \frac{n_{10} - n_{01}}{N}$

The standard Wald asymptotic variance of $\text{RD} = p_1 - p_2$ (Agresti, Categorical Data Analysis; Fleiss, Statistical Methods for Rates and Proportions) is:
$$\operatorname{Var}(p_1 - p_2) = \frac{p_1(1 - p_1) + p_2(1 - p_2) - 2\operatorname{Cov}(Y_1, Y_2)}{N}$$

Expressed in contingency table counts, the covariance term is:
$$\operatorname{Cov}(Y_1, Y_2) = p_{11} - p_1 p_2 = \frac{n_{11} n_{00} - n_{10} n_{01}}{N^2}$$

Substituting into the full variance expression:
$$\operatorname{Var}(p_1 - p_2) = \frac{(n_{11}+n_{10})(n_{01}+n_{00}) + (n_{11}+n_{01})(n_{10}+n_{00}) - 2(n_{11} n_{00} - n_{10} n_{01})}{N^3}$$

Algebraic simplification of the correct numerator yields:
$$(n_{11}+n_{10})(n_{01}+n_{00}) + (n_{11}+n_{01})(n_{10}+n_{00}) - 2(n_{11} n_{00} - n_{10} n_{01}) = N(n_{10} + n_{01}) - (n_{10} - n_{01})^2$$

## 3. The Sign Discrepancy

In the decider code, the third term inside `var` is written as:
`- 2*(n01*n10 - n11*n00)`

Because the inner subtraction is $(n_{01} n_{10} - n_{11} n_{00})$ instead of $(n_{11} n_{00} - n_{10} n_{01})$, distributing the negative sign yields:
$$- 2(n_{01} n_{10} - n_{11} n_{00}) = + 2(n_{11} n_{00} - n_{10} n_{01})$$

The covariance term is therefore **added** to the independent variances rather than **subtracted**.

## 4. Arithmetic for Contrast C1 (lesson vs nothing)

Counts for C1 across all $N = 133$ pairs:
* $n_{11} = 28$
* $n_{10} = 38$
* $n_{01} = 0$
* $n_{00} = 67$
* $N = 133$ ($N^3 = 2,352,637$)

### Component Calculations
* Term 1: $(n_{11} + n_{10})(n_{01} + n_{00}) = (28 + 38)(0 + 67) = 66 \times 67 = 4,422$
* Term 2: $(n_{11} + n_{01})(n_{10} + n_{00}) = (28 + 0)(38 + 67) = 28 \times 105 = 2,940$
* Sum of variance components: $4,422 + 2,940 = 7,362$
* Determinant: $n_{11} n_{00} - n_{10} n_{01} = (28 \times 67) - (38 \times 0) = 1,876 - 0 = 1,876$
* Twice covariance: $2 \times 1,876 = 3,752$

### Correct Formula (Subtracting Covariance)
* Numerator: $7,362 - 3,752 = 3,610$
* Variance: $\operatorname{Var} = \frac{3,610}{2,352,637} \approx 0.001534448$
* Standard Error: $\text{SE} = \sqrt{0.001534448} \approx 0.039172 \to \mathbf{0.0392}$
* $z \times \text{SE} = 1.96 \times 0.039172 \approx 0.076777$
* $\text{RD} = \frac{38 - 0}{133} \approx 0.285714$
* 95% Confidence Interval: $[0.285714 - 0.076777, 0.285714 + 0.076777] = \mathbf{[0.209, 0.362]}$

### As Coded in analysis_decider.py (Adding Covariance)
* Numerator: $7,362 + 3,752 = 11,114$
* Variance: $\operatorname{Var} = \frac{11,114}{2,352,637} \approx 0.00472406$
* Standard Error: $\text{SE} = \sqrt{0.00472406} \approx 0.0687318 \to \mathbf{0.0687}$
* $z \times \text{SE} = 1.96 \times 0.0687318 \approx 0.134714$
* 95% Confidence Interval: $[0.285714 - 0.134714, 0.285714 + 0.134714] = \mathbf{[0.151, 0.420]}$

## 5. Statistical Implication

Because the data exhibits strong positive concordance ($n_{11} n_{00} = 1876 > 0$), adding covariance instead of subtracting it artificially inflates the estimated variance ($11,114$ vs $3,610$). 

This sign error **widens the confidence interval** (width $0.269$ vs $0.153$), shifting the lower bound downwards ($0.151$ vs $0.209$). Consequently, the coded formula **understates our confidence** rather than overstating it.

## 6. Pre-Registration Status

The decider script is pre-registered and its SHA-256 (`964fc365a27593cb254097f9df404036cbbf7a605c5c8906e277fd752afe1db9`) is frozen. Changing the decider code post-data invalidates the pre-registration contract. The pre-registered interval $[0.151, 0.420]$ remains the primary pre-registered result; the corrected interval $[0.209, 0.362]$ is disclosed as sensitivity analysis.
