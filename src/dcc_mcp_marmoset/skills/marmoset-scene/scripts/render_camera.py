from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(
    path: str,
    width: int = -1,
    height: int = -1,
    sampling: int = -1,
    transparency: bool = False,
    camera: str = "",
    **_kwargs,
):
    result = call_host(
        "render.camera",
        {
            "path": path,
            "width": width,
            "height": height,
            "sampling": sampling,
            "transparency": transparency,
            "camera": camera,
        },
    )
    return skill_success("Marmoset Toolbag camera rendered.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
