from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(**_kwargs):
    return skill_success(
        "Marmoset Toolbag released unused resources.",
        **call_host("diagnostics.free_unused_resources", timeout=60.0),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
