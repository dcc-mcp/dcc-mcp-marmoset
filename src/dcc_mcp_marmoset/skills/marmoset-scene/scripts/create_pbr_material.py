from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(
    material_name: str,
    object_uid: str,
    base_color_path: str,
    normal_path: str,
    roughness_path: str,
    metalness_path: str,
    occlusion_path: str,
    include_children: bool = True,
    **_kwargs,
):
    return skill_success(
        "PBR material created and assigned in Marmoset Toolbag.",
        **call_host(
            "material.create_pbr",
            {
                "material_name": material_name,
                "object_uid": object_uid,
                "include_children": include_children,
                "base_color_path": base_color_path,
                "normal_path": normal_path,
                "roughness_path": roughness_path,
                "metalness_path": metalness_path,
                "occlusion_path": occlusion_path,
            },
            timeout=120.0,
        ),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
