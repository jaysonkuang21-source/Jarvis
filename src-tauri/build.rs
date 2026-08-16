fn main() {
    // Explicitly register app commands so ACL permissions are always generated
    // (prevents "Permission allow-get-api-token not found" build failures).
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["get_api_token"]),
        ),
    )
    .expect("failed to run tauri-build");
}
