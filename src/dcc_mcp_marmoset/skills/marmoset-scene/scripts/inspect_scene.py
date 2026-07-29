from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host
from dcc_mcp_marmoset.server import publish_scene_snapshot


@skill_entry
def main(max_objects: int = 500, **_kwargs):
    result = call_host("scene.inspect", {"max_objects": max_objects})
    publish_scene_snapshot(result)
    return skill_success("Marmoset Toolbag scene inspected.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
