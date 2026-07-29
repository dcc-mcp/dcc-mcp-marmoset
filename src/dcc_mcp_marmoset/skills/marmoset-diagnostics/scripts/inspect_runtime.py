from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(**_kwargs):
    return skill_success(
        "Marmoset Toolbag runtime inspected.",
        **call_host("diagnostics.inspect_runtime"),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
