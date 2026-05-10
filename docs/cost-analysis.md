# RunPod Volume vs Cold-Start Cost Analysis

This document captures a simple breakeven calculation for whether to pay for a
persistent RunPod volume versus accepting cold-start download time on each Pod
start.

## Scenario

Assumptions:

- volume size: `50 GB`
- volume pricing: `$0.20/GB/month`
- GPU pricing: `$0.69/hour` (RTX 4090 Secure Cloud)
- extra cold-start download time without a persistent volume: `10 minutes`

## Monthly Volume Cost

Formula:

`volume_cost_month = volume_gb * volume_price_per_gb_month`

Calculation:

`50 * 0.20 = $10.00/month`

## Cold-Start Compute Cost

Formula:

`cold_start_cost = gpu_hourly_price * (cold_start_minutes / 60)`

Calculation:

`0.69 * (10/60) = $0.115` per Pod start

## Breakeven Point

Breakeven starts per month:

`breakeven_starts = volume_cost_month / cold_start_cost`

Calculation:

`10.00 / 0.115 = 86.96`

Rounded:

- about `87` starts/month

## Conclusion

With these assumptions:

- if Pod starts are below about `87` per month, `0 GB` volume is cheaper
- at about `87` starts/month, costs are roughly equal
- above `87` starts/month, paying for persistent volume becomes cheaper

## Reference Pricing

- RunPod RTX 4090 pricing: <https://www.runpod.io/gpu/4090>
- RunPod storage pricing: <https://www.runpod.io/pricing>
