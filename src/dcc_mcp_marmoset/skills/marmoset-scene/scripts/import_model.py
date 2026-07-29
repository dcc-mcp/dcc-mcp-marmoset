from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(path: str, **_kwargs):
    return skill_success(
        "Model imported into Marmoset Toolbag.",
        **call_host("scene.import_model", {"path": path}, timeout=120.0),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
