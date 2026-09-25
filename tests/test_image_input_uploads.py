import asyncio

import pytest

from src.services.generation_handler import GenerationHandler


@pytest.mark.asyncio
async def test_reference_uploads_overlap_but_preserve_input_order():
    handler = GenerationHandler.__new__(GenerationHandler)
    active = 0
    peak = 0
    two_started = asyncio.Event()

    async def upload_one(token, project_id, model_config, image_bytes):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            two_started.set()
        await two_started.wait()
        await asyncio.sleep(0)
        active -= 1
        return image_bytes.decode()

    handler._upload_reference_image = upload_one
    result = await asyncio.wait_for(
        handler._upload_image_inputs(None, "project", {}, [b"first", b"second", b"third"]),
        timeout=1,
    )

    assert peak == 2
    assert [item["name"] for item in result] == ["first", "second", "third"]
    assert all(item["imageInputType"] == "IMAGE_INPUT_TYPE_REFERENCE" for item in result)
