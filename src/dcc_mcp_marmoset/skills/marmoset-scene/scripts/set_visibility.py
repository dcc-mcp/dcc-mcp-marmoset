from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(object_uids: list[str], visible: bool, **_kwargs):
    return skill_success(
        "Toolbag object visibility updated.",
        **call_host(
            "scene.set_visibility",
            {"object_uids": object_uids, "visible": visible},
            timeout=30.0,
        ),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
