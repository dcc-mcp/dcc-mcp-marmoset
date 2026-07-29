from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(max_materials: int = 500, **_kwargs):
    return skill_success(
        "Marmoset Toolbag texture references validated.",
        **call_host("diagnostics.validate_assets", {"max_materials": max_materials}),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
