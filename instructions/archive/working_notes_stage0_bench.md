# Stage 0 — labelled-ads bench (working notes)

Survey of 1947-02-27 and 1947-11-06, both 0% hand-edited.

**Method**: rendered full-resolution overlays at `screenshots/exploration/bench_<issue>_pN.png` showing current DB detections in BLUE solid (D1, D2…) and cleanup-pipeline candidates in ORANGE dashed (C1, C2…). Walked each page and recorded only OBVIOUS misses (clear bordered display ads with no D/C box) and OBVIOUS FPs (oversized merge boxes, text-block-as-ad detections). Marginal cases skipped — user verifies in batch.

**Per-page D / C counts**:

| | 1947-02-27 | 1947-11-06 |
|---|---|---|
| p1 | 0 / 1 | 0 / 1 |
| p2 | 2 / 0 | 6 / 4 |
| p3 | 6 / 7 | 4 / 5 |
| p4 | 10 / 12 | 8 / 7 |
| p5 | 7 / 6 | 4 / 6 |
| p6 | 3 / 3 | 3 / 4 |
| p7 | 1 / 6 | 1 / 1 |
| p8 | 9 / 8 | 8 / 8 |
| total | 38 / 43 | 34 / 36 |

---

## 1947-02-27 — fast-pass observations

### p1 (front page)
- **No real ads.** C1 is likely the masthead (front-page art / nameplate). Cleanup-pipeline FP signature.
- **Action proposal**: nothing to label (no D rows; no MISS).

### p2 — TWO LIKELY FPs and ONE CLEAR MISS
- **D1 low** (36.4, 33.5, 16.2, 9.4) — looks like editorial/text-block at "BILL EDITORIAL BROADCAST" content. **Likely FP** (text-block-as-ad).
- **D2 low** (36.1, 44.3, 16.2, 11.4) — adjacent text-block at "Color Bait" / similar. **Likely FP** (text-block-as-ad).
- **MISS**: Bottom-right "Bank of Montreal — Why not start a business of your own?" — clearly bordered display ad, neither D nor C catches it. Position ≈ (75, 60, 22, 32).

### p3 — ONE BIG FP, ONE LIKELY MISS
- **D3 low** (64.0, 21.3, 27.7, 62.6) — huge box covering top→bottom of right half of page. **Clear FP**: frame-merge / outer-box false positive that swallows several adjacent ads inside it. Likely the "frame-pair" pattern from p8 investigation.
- **MISS** likely: Left column "The Red Cross Carries On / The need of money never ends. Give" — bordered display ad on left. Faint D/C presence — UNCERTAIN.
- Other detections look reasonable (D2 Pasto Fertilize, D5 Brading's Good Citizenship, D6 Super-Pyro / North Lanark Mart).

### p4 — classifieds-heavy, no obvious issues
- 10 D, 12 C. Both detectors find a similar dense set of ads (Karl/James grocery, Mineralia Coffee, Spy Apples, Auction Sales, Sneddon's Drug, T.M. Coady, R. Eldon Brown, etc.).
- No obvious misses; FP risk: D may be over-detecting Auction Sales notices, but those are arguably small classified ads.

### p5 — heavy ad column on right
- D1 + D2 + D5 + D6 + D7 cover Star Theatre + Salada + W. Halpenny + Gomba's + various.
- No obvious miss on first scan.

### p6 — utility / Hydro ads
- D1 "Never touch a fallen Wire!", D2 "Hydro Lamps DuFresne", D3 "Tom! What can I say? Labatt's" all detected.
- **POSSIBLE MISS**: mid-page-left "On the Road / Dr. Chase's Kidney-Liver Pills" — looks bordered, may not be detected. UNCERTAIN.

### p7 — 1 D vs 6 C disparity
- Page is mostly news columns. Few visible ads. The 6 C candidates are likely a mix of:
  - bordered editorial blocks (HIGH SCHOOL BOWL CONTEST, MINING BOWL headers)
  - calendar tables (FEBRUARY block at bottom)
  - genuine small ads (Canadian Garden Service, etc.)
- **Likely scenario**: cleanup pipeline is over-finding on this news-heavy page. UNCERTAIN whether C1-C6 are TPs or FPs without per-box inspection.

### p8 — tight agreement, well-covered
- 9 D / 8 C, almost full overlap (verified earlier in CV investigation).
- D7 low-confidence "Steel's Grill / OPEN FROM 8.30" — present in D, missing in C. Probably real but marginal.
- No obvious MISS or FP after detailed earlier review.

---

## 1947-11-06 — fast-pass observations

### p1 (front page)
- No real ads. C1 likely masthead.
- **Action proposal**: nothing.

### p2 — looks reasonable
- 6 D / 4 C. "Vicks Vapo-Rub", "Moto-Master Super Anti-Freeze", "Associate Store", "Local Items of Fifty Years Ago" template ad, etc. — all visible, mostly detected.
- No obvious MISS on fast scan.

### p3 — Bank of Montreal recurring template
- D1 "Look, Daddy... Bank of Montreal" (top-left) — same template as 1947-02-27 p2's MISS, but here it IS detected.
- D3 "Brading's Good Citizenship" (bottom-left) — detected (recurring template).
- D4 "Comba's / R. Eldon Brown" — multiple right-side ads.
- **POSSIBLE MISS**: small "The Business of Pioneers Travel" mid-page-right. UNCERTAIN.

### p4 — classifieds-heavy
- 8 D / 7 C. Karl's Grocery / Heinz Tomato Soup / Honey / Fresh Fruits & Veg / Ottawa Winter Fair / Marry's Grill / Stop Pain Headaches / Smith's Twelve Cent Bread / Jas. F. Patterson / Jin / various classifieds covered.
- No obvious MISS.

### p5 — 4 D vs 6 C disparity
- D coverage looks light: Star Theatre + Welcome Stranger + Almonte Food Market + ??.
- **POSSIBLE MISSES** that cleanup may catch (4 vs 6):
  - "Salada Tea Bags" (top right) — recurring template
  - "Royal Winter Fair / Ottawa" (mid-right)
  - "Harness Parts" (mid-left)
  - "Live Poultry Receiving Station / R. North Lanark Dist. Co-Operative" (bottom-left)
- All look bordered, likely real ads. **MISS candidates** worth investigating per-box.

### p6 — 3 D vs 4 C
- D2 "Did You Pay Income Tax for 1942 / Department of National Revenue" — detected.
- D3 "3 Way Action / Dr. Chase's Kidney-Liver Pills" — detected.
- "Royal Winter Fair / Coliseum" bottom-left — detected.
- **POSSIBLE MISS**: "Tomberlo Hatters / Now Available" or "Pakistan Times of Distinction A.G. Naismith & Son" mid-right. UNCERTAIN.

### p7 — news page, low ad count plausibly correct
- 1 D / 1 C. Page is dense news (Lanark County Federation, Committee Report, Local Legion Auxiliary, etc.). The single detection looks correct ("MIXING BOWL" or similar at top).

### p8 — heavy display ad page
- 8 D / 8 C, full overlap probable. "Reduced Prices Almonte Garage", "Legion Calendar of Social Events", "Rexall One-Cent Sale Snedden", "Famous Cooks!", "J.H. Martin Electric Washing Machines", "DOMINION groceries" all detected.
- No obvious MISS or FP.

---

## Summary of obvious findings

**Confirmed MISSes** (after user verification 2026-04-28):

| Issue | Page | Description | Class | Stage target |
|---|---|---|---|---|
| 1947-02-27 | p2 | Bank of Montreal — "business of your own?" bottom-right | whitespace-only | Stage 4 |
| 1947-02-27 | p3 | "Red Cross Carries On" left column | whitespace-only | Stage 4 |
| 1947-02-27 | p6 | Dr. Chase's Kidney-Liver Pills mid-left | bordered | **Stage 2** |
| 1947-11-06 | p5 | Salada Tea Bags top-right — consistently missed across issues | whitespace-only | Stage 4 |
| 1947-11-06 | p5 | Royal Winter Fair / Ottawa | bordered | **Stage 2** |
| 1947-11-06 | p5 | Harness Parts | bordered | **Stage 2** |
| 1947-11-06 | p5 | Live Poultry / North Lanark Co-Op | bordered | **Stage 2** |
| 1947-11-06 | p6 | Top-right corner ad (Tomberlo + Naismith are ONE big ad, not two) | bordered | **Stage 2** |

**Stage-classified totals**:
- Stage 2 (bordered MISSes — cleanup pipeline recall target): **~10** (Dr. Chase's; Royal Winter Fair; Harness Parts; Live Poultry; Tomberlo+Naismith corner ad; **5 cleanup-pipeline-only candidates on 1947-02-27 p7** that user confirms should have been included)
- Stage 4 (whitespace MISSes — research, no commitment): **3** (Bank of Montreal p2, Red Cross p3, Salada p5)

**Note on Salada**: user flagged as "consistently missed" — recurring template across issues. Worth checking whether this template appears on 1940-02-20 too (which has hand_edited truth), as a cross-issue Stage-4 recall datapoint.

**Note on Tomberlo/Naismith**: my fast-pass framed these as "Tomberlo or Naismith" — two candidates. User clarifies they're ONE big ad in the top-right corner. The current detection state for that single bbox needs checking; could be fully missed or partially split.

**Likely MISSes (need user verification)**:
| Issue | Page | Description |
|---|---|---|
| 1947-02-27 | p3 | "Red Cross Carries On" left column |
| 1947-02-27 | p6 | "Dr. Chase's Kidney-Liver Pills" mid-left |
| 1947-11-06 | p5 | Salada / Royal Winter Fair / Harness Parts / Live Poultry — 4 candidates that 6 C boxes may already cover |
| 1947-11-06 | p6 | Tomberlo Hatters or A.G. Naismith |

**Clear FPs (cli_history delete candidates)**:
*(none confirmed yet — see correction below)*

**Corrected understanding of FP framing** (user feedback 2026-04-28):

I initially flagged three rows on 1947-02-27 as FPs but the correct framing is "what reaches final output", not what detect_ads emits. The body-text FP filter (`process_issue.py:759-789`) and other downstream gates already handle several of these correctly. Updated assessment:

- **p2 D1 (bde5bf9d)** and **D2 (c123762c)** — body-text FP filter catches these downstream. NOT bench FPs.
- **p3 D3 (81300757)** — actually approximates a real ad region: the Centennial ad sits in whitespace on the right side of p3, so D3's bbox isn't far wrong. Downstream filtering removes it. NOT a bench FP.

Bench-relevant FPs are detections that survive all production filtering and still surface as wrong. Future user passes will surface those.

**Resolved: 1947-02-27 p7 — cleanup-pipeline recoveries, not FPs** (user verification 2026-04-28):
- 1 D vs 6 C disparity. The C-only candidates (including a calendar/table block the user explicitly named) should have been included; current production missing them is the bug, cleanup pipeline picking them up is correct behaviour.
- Treat the 5 extra cleanup-pipeline candidates as **bordered MISSes** for Stage 2 recall accounting. Per-C-box bbox extraction can come at promotion time.

---

## Bench size assessment vs plan acceptance criterion

Plan target: ≥30 ads + ≥10 FPs total in the bench across ≥3 issues.

Current state I can confirm:
- 1940-02-20 — 13 hand_edited rows already (TPs), 12 cli_history deletes (FPs from prior tidy pass).
- 1947-02-27 (this survey): 3 confirmed FPs, 1 confirmed MISS, ~3 likely MISSes pending user verification.
- 1947-11-06 (this survey): 0 confirmed FPs, 0 confirmed MISSes, ~5 likely MISSes pending user verification.

**Counting only currently-confirmed**:
- TPs: ~30 (from 1940-02-20 hand_edited + spot-check on 1947 pages where D and C agree)
- FPs: 12 (1940-02-20 cli_history) + 3 (1947-02-27 above) = 15
- MISSes: 1 confirmed + ~8 likely

Acceptance criterion is met for FPs (15 ≥ 10). MISSes are the lighter class — Stage 2's whole purpose is to recover them, so the bench needs more. Suggest the user verifies the "likely MISSes" list above to bring confirmed misses to ≥5-8.

---

## Proposed next actions (no DB writes yet)

1. **You verify the "Likely MISSes" and "Uncertain" rows above** by spot-checking the bench overlay PNGs in `screenshots/exploration/bench_<issue>_pN.png`.
2. Once verified, I write a small script to:
   - Set `hand_edited=1` on the D rows the user confirmed as TPs (no DB change required for "this is correct" — confidence in current row implies hand_edited).
   - Insert new `detected_ads` rows with `hand_edited=1` and bboxes for confirmed MISSes (bbox extracted from cleanup-pipeline candidate where C exists; user supplies for cases where neither D nor C found it, like the Bank of Montreal on 1947-02-27 p2).
   - Insert `cli_history` delete records for confirmed FPs (matches existing pattern from 1940-02-20 tidy pass).
3. After bench is in DB: ready for Stage 1 (extract `page_cv.py`).

---

## Files

Bench overlays (all under `/Users/peter/Projects/MVTM/screenshots/exploration/`):
- `bench_1947-02-27_p1.png` … `bench_1947-02-27_p8.png`
- `bench_1947-11-06_p1.png` … `bench_1947-11-06_p8.png`

Rendering scripts (throwaway, in `/tmp`):
- `bench_render_p8.py` — single-page version
- `bench_render_all.py` — full-issue version
