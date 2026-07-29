from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(max_materials: int = 200, **_kwargs):
    return skill_success(
        "Marmoset Toolbag materials inspected.",
        **call_host("material.inspect", {"max_materials": max_materials}, timeout=60.0),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
