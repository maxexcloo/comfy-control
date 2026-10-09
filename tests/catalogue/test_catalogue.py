from pathlib import Path

from catalogue.workflows import Catalogue

ROOT = Path(__file__).parents[2]


def test_catalogue_loads_and_renders_workflow():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get("flux-2-klein-9b")
    graph = model.render({"prompt": "a red fox", "seed": 42, "width": 768})

    assert model.id == "flux-2-klein-9b/text-to-image"
    assert graph["4"]["inputs"]["text"] == "a red fox"
    assert graph["6"]["inputs"]["width"] == 768
    assert graph["7"]["inputs"]["noise_seed"] == 42


def test_catalogue_maps_one_parameter_to_multiple_nodes():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    graph = catalogue.get("flux-2-klein-9b").render({"height": 768, "width": 1344})

    assert graph["6"]["inputs"]["height"] == 768
    assert graph["6"]["inputs"]["width"] == 1344
    assert graph["9"]["inputs"]["height"] == 768
    assert graph["9"]["inputs"]["width"] == 1344


def test_catalogue_maps_image_edit_inputs_to_reference_workflow():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    graph = catalogue.get("flux-2-klein-9b-edit").render(
        {"image": "input.png", "prompt": "change the sky", "seed": 7, "steps": 8}
    )

    assert graph["8"]["inputs"]["text"] == "change the sky"
    assert graph["4"]["inputs"]["image"] == "input.png"
    assert graph["13"]["inputs"]["noise_seed"] == 7
    assert graph["15"]["inputs"]["steps"] == 8
    assert graph["10"]["class_type"] == "ReferenceLatent"
    assert graph["11"]["class_type"] == "ReferenceLatent"


def test_catalogue_maps_image_to_video_first_frame():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    graph = catalogue.get("minimax-h3-i2v").render(
        {
            "height": 768,
            "image": "frame.png",
            "length": 124,
            "prompt": "walk forward",
            "seed": 3,
            "steps": 16,
            "width": 1344,
        }
    )

    assert graph["5"]["class_type"] == "LoadImage"
    assert graph["5"]["inputs"]["image"] == "frame.png"
    assert graph["6"]["class_type"] == "MiniMaxH3ImageToVideo"
    assert graph["6"]["inputs"]["first_frame"] == ["5", 0]
    assert graph["6"]["inputs"]["prompt"] == "walk forward"
    assert graph["10"]["inputs"]["noise_seed"] == 3
    assert graph["8"]["inputs"]["steps"] == 16


def test_catalogue_maps_image_upscale_inputs():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    graph = catalogue.get("image-upscale").render({"image": "source.png", "scale": 3.5})

    assert graph["1"]["inputs"]["image"] == "source.png"
    assert graph["2"]["inputs"]["model_name"] == "RealESRGAN_x4plus.safetensors"
    assert graph["3"]["inputs"]["image"] == ["1", 0]
    assert graph["4"]["inputs"]["scale_by"] == 0.875


def test_catalogue_exposes_alias_only_once():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    assert [model.id for model in catalogue.list()] == [
        "flux-2-klein-9b/image-edit",
        "flux-2-klein-9b/text-to-image",
        "image-upscale/realesrgan-x4plus",
        "krea-2-turbo/text-to-image",
        "minimax-h3/image-to-video",
        "minimax-h3/text-to-video",
    ]


def test_catalogue_only_exposes_workflows_with_required_models(tmp_path):
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get("flux-2-klein-9b")
    model.required_files = ["checkpoints/test.safetensors"]

    assert model.missing_files(tmp_path) == ["checkpoints/test.safetensors"]
    assert catalogue.list_available(tmp_path) == []

    checkpoint = tmp_path / "checkpoints/test.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"model")

    assert [item.id for item in catalogue.list_available(tmp_path)] == [
        "flux-2-klein-9b/text-to-image"
    ]

    upscaler = tmp_path / "upscale_models/RealESRGAN_x4plus.safetensors"
    upscaler.parent.mkdir()
    upscaler.write_bytes(b"model")

    assert [item.id for item in catalogue.list_available(tmp_path)] == [
        "flux-2-klein-9b/text-to-image",
        "image-upscale/realesrgan-x4plus",
    ]


def test_catalogue_hides_workflows_with_unregistered_nodes(tmp_path):
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get("flux-2-klein-9b")
    model.required_files = []

    object_info = {node["class_type"]: {} for node in model._graph.values()}

    assert model.missing_nodes(object_info) == []
    assert catalogue.list_available(tmp_path, object_info) == [model]

    assert model.missing_nodes({}) == sorted(object_info)
    assert catalogue.list_available(tmp_path, {}) == []


def test_catalogue_requires_models_and_nodes_together(tmp_path):
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get("flux-2-klein-9b")
    model.required_files = ["checkpoints/test.safetensors"]

    checkpoint = tmp_path / "checkpoints/test.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"model")

    assert (
        catalogue.get_available(
            "flux-2-klein-9b",
            tmp_path,
            {node["class_type"]: {} for node in model._graph.values()},
        ).id
        == "flux-2-klein-9b/text-to-image"
    )
    try:
        catalogue.get_available("flux-2-klein-9b", tmp_path, {})
    except KeyError as exc:
        assert "unregistered nodes" in str(exc)
    else:
        raise AssertionError("expected KeyError for unregistered nodes")
