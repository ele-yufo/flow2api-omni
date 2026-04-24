# Restore I2V Standard Model + 16s Extend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the previously removed `veo_3_1_i2v_s` standard I2V model and add its 16s extend variant, enabling 满血版 (standard/quality) I2V generation with video extension.

**Architecture:** The extend flow reuses the existing `_poll_video_result` extend+concatenate pipeline. The extend model key is universal (`veo_3_1_extend_landscape`/`veo_3_1_extend_portrait`) — same as all fast/ultra/lite variants. No new Flow API methods needed. Changes are purely config additions in MODEL_CONFIG and VIDEO_BASE_MODELS.

**Tech Stack:** Python, existing FastAPI service, Google Flow API (unchanged)

---

## File Structure

| File | Change | Description |
|------|--------|-------------|
| `src/services/generation_handler.py` | Modify L452-453, L920 | Restore 2 base I2V entries + add 2 _16s entries to MODEL_CONFIG |
| `src/core/model_resolver.py` | Modify L185, L241 | Add `veo_3_1_i2v_s` base + `_16s` aliases to VIDEO_BASE_MODELS |
| `README.md` | Modify I2V tables | Add restored models to documentation |

---

### Task 1: Restore I2V standard base models in MODEL_CONFIG

**Files:**
- Modify: `src/services/generation_handler.py` (insert after line 452, within I2V section)

The standard I2V model (`veo_3_1_i2v_s`) was previously removed because it returned 500. HAR capture from the Flow web UI confirms it works. The upstream model key for landscape is `veo_3_1_i2v_s` (HAR confirmed) and for portrait is `veo_3_1_i2v_s_portrait` (following existing naming convention).

- [ ] **Step 1: Insert base model entries after the existing `veo_3_1_i2v_s_fast_ultra_relaxed` entries**

Insert the following block after line 451 (after `veo_3_1_i2v_s_fast_ultra_relaxed` landscape entry, before the `veo_3_1_i2v_lite` section):

```python

    # veo_3_1_i2v_s 满血版 (标准 I2V, 支持 1-2 张图片)
    "veo_3_1_i2v_s_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "allow_tier_upgrade": False
    },
    "veo_3_1_i2v_s_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "allow_tier_upgrade": False
    },
```

Key details:
- `model_key` for landscape is `"veo_3_1_i2v_s"` (HAR confirmed — no `_landscape` suffix)
- `model_key` for portrait is `"veo_3_1_i2v_s_portrait"` (following T2V/I2V naming convention)
- `max_images: 2` — supports both single-frame and first-last-frame I2V
- `allow_tier_upgrade: False` — prevents auto-upgrading to ultra

- [ ] **Step 2: Verify no syntax errors**

Run: `python3 -c "from src.services.generation_handler import MODEL_CONFIG; print(f'Total models: {len(MODEL_CONFIG)}')"`
Expected: No errors, total model count increased by 2.

---

### Task 2: Add I2V standard 16s extend variants to MODEL_CONFIG

**Files:**
- Modify: `src/services/generation_handler.py` (insert within the 16s I2V section, around line 920)

- [ ] **Step 1: Insert 16s entries after the existing I2V 16s section**

Find the last I2V 16s entry (`veo_3_1_interpolation_lite_landscape_16s`) and insert after it (before the R2V 16s section):

```python

    # I2V 满血版 延长 16s
    "veo_3_1_i2v_s_portrait_16s": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "allow_tier_upgrade": False,
        "extend": {"model_key": "veo_3_1_extend_portrait"}
    },
    "veo_3_1_i2v_s_landscape_16s": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "allow_tier_upgrade": False,
        "extend": {"model_key": "veo_3_1_extend_landscape"}
    },
```

Key details:
- Same `model_key` as base entries (Task 1)
- `"extend": {"model_key": "veo_3_1_extend_portrait"}` / `"veo_3_1_extend_landscape"` — universal extend keys
- The `_poll_video_result` method will automatically detect the `extend` config and run the extend+concatenate pipeline

- [ ] **Step 2: Verify no syntax errors**

Run: `python3 -c "from src.services.generation_handler import MODEL_CONFIG; assert 'veo_3_1_i2v_s_portrait_16s' in MODEL_CONFIG; assert 'veo_3_1_i2v_s_landscape_16s' in MODEL_CONFIG; print('OK')"`
Expected: `OK`

---

### Task 3: Add VIDEO_BASE_MODELS aliases in model_resolver.py

**Files:**
- Modify: `src/core/model_resolver.py` (within VIDEO_BASE_MODELS dict)

- [ ] **Step 1: Add base model alias after existing I2V entries (after line 185, `veo_3_1_interpolation_lite`)**

Insert:

```python
    "veo_3_1_i2v_s": {
        "landscape": "veo_3_1_i2v_s_landscape",
        "portrait": "veo_3_1_i2v_s_portrait",
    },
```

- [ ] **Step 2: Add 16s alias after existing I2V 16s entries (after line 241, `veo_3_1_interpolation_lite_16s`)**

Insert:

```python
    "veo_3_1_i2v_s_16s": {
        "landscape": "veo_3_1_i2v_s_landscape_16s",
        "portrait": "veo_3_1_i2v_s_portrait_16s",
    },
```

- [ ] **Step 3: Verify resolver works**

Run: `python3 -c "from src.core.model_resolver import resolve_model; r = resolve_model('veo_3_1_i2v_s'); print(r); r2 = resolve_model('veo_3_1_i2v_s_16s'); print(r2)"`
Expected: Both resolve without errors.

---

### Task 4: Update README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add `veo_3_1_i2v_s` entries to the I2V model table**

In the I2V model table, add rows for:
- `veo_3_1_i2v_s_portrait` — I2V 满血版 竖屏
- `veo_3_1_i2v_s_landscape` — I2V 满血版 横屏

- [ ] **Step 2: Add `veo_3_1_i2v_s` 16s entries to the 16s extend table**

In the I2V 16s model table, add rows for:
- `veo_3_1_i2v_s_portrait_16s` — I2V 满血版 竖屏 16s
- `veo_3_1_i2v_s_landscape_16s` — I2V 满血版 横屏 16s

---

### Task 5: Restart and E2E verification

**Files:** None (testing only)

- [ ] **Step 1: Restart the service**

Run: `sudo systemctl restart flow2api.service && sleep 3 && systemctl is-active flow2api.service`
Expected: `active`

- [ ] **Step 2: Test base I2V standard model resolution**

Run: `curl -s 'http://localhost:18282/v1/models' -H 'Authorization: Bearer han1234' | python3 -c "import json,sys; d=json.load(sys.stdin); models=[m['id'] for m in d['data']]; print('i2v_s_portrait' in ' '.join(models)); print('i2v_s_landscape_16s' in ' '.join(models))"`
Expected: Both `True`

- [ ] **Step 3: Commit**

```bash
git add src/services/generation_handler.py src/core/model_resolver.py README.md
git commit -m "feat(video): restore veo_3_1_i2v_s standard I2V model with 16s extend support"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** Restore `veo_3_1_i2v_s` base + 16s — covered in Tasks 1-2
- [x] **Placeholder scan:** No TBD/TODO/fill-in-later found
- [x] **Type consistency:** `model_key` values match between base and _16s entries; extend model keys match existing convention
- [x] **No new API methods needed:** extend_video(), concatenate_videos(), check_concatenation_status() already exist
- [x] **Upsample+extend combo:** Not applicable — standard I2V has no upsample variant (only fast_ultra has that)
