// dump_driver.cpp — dlopen the unpacked Linux x86_64 libbambu_networking.so
// under qemu-x86_64 and drive it through the cert-installation flow so
// that whatever it generates / writes to disk / sends over the wire is
// observable from outside (via the LD_PRELOAD trace shim).
//
// Build: zig c++ -target x86_64-linux-gnu -O1 dump_driver.cpp -ldl -o dump_driver
//
// Run:   LD_PRELOAD=./trace_shim.so \
//        BAMBU_TRACE_LOG=/path/to/trace.log \
//        LD_LIBRARY_PATH=/path/to/bambu_plugin/linux \
//        qemu-x86_64 -L /path/to/x86_64-sysroot ./dump_driver \
//          /path/to/libbambu_networking.so <access_token> <user_id>
//
// Goal: trigger bambu_network_install_device_cert(...) with a valid
// cloud-account token. The dylib then:
//   1. generates an RSA-2048 keypair locally (capture via [open] of
//      the new key file or via OpenSSL EVP hooks if libcrypto is
//      dynamically linked)
//   2. signs the request with its embedded VMProtect-wrapped bootstrap
//      private key (we don't need to crack this — we let the dylib do
//      it, capturing the resulting headers via curl_slist_append hook)
//   3. POSTs to /v1/iot-service/api/user/applications/<app>/cert
//   4. decrypts the response (AES-256 wrapped with our supplied key)
//   5. writes the resulting per-install cert + key to the config_dir
//      we set in step 2 of this driver — capture via [open]/[write].

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <unistd.h>
#include <dlfcn.h>

using create_agent_fn        = void* (*)(std::string);
using set_config_dir_fn      = int   (*)(void*, std::string);
using change_user_fn         = int   (*)(void*, std::string);
using install_device_cert_fn = void  (*)(void*, std::string, bool);
using set_country_code_fn    = int   (*)(void*, std::string);
using start_fn               = int   (*)(void*);
using init_log_fn            = int   (*)(void*);
using is_user_login_fn       = bool  (*)(void*);
using get_version_fn         = std::string (*)();
using set_cert_file_fn       = int   (*)(void*, std::string, std::string);

template<class F>
static F resolve_or_warn(void* h, const char* name) {
    F f = reinterpret_cast<F>(dlsym(h, name));
    if (!f) {
        fprintf(stderr, "[driver] WARN: dlsym(%s) returned NULL: %s\n",
                name, dlerror());
    }
    return f;
}

int main(int argc, char** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    setvbuf(stderr, nullptr, _IONBF, 0);

    if (argc < 4) {
        fprintf(stderr,
            "usage: %s <libbambu_networking.so> <access_token> <user_id>\n"
            "\n"
            "  Set BAMBU_CONFIG_DIR  to override config dir (default /tmp/bambu_certs)\n"
            "  Set BAMBU_LOG_DIR     to override log dir    (default /tmp/bambu_log)\n"
            "  Set BAMBU_DEV_ID      to install device cert for a specific dev_id\n"
            "                        (default empty → user-account cert)\n"
            "  Set BAMBU_LAN_ONLY=1  to call install_device_cert(..., lan_only=true)\n",
            argv[0]);
        return 2;
    }

    const char* so_path = argv[1];
    const char* token   = argv[2];
    const char* user_id = argv[3];
    const char* config_dir = getenv("BAMBU_CONFIG_DIR");
    const char* log_dir    = getenv("BAMBU_LOG_DIR");
    const char* dev_id     = getenv("BAMBU_DEV_ID");
    bool lan_only          = getenv("BAMBU_LAN_ONLY") != nullptr;

    if (!config_dir) config_dir = "/tmp/bambu_certs";
    if (!log_dir)    log_dir    = "/tmp/bambu_log";
    if (!dev_id)     dev_id     = "";

    fprintf(stderr, "[driver] dlopen(%s) ...\n", so_path);
    void* h = dlopen(so_path, RTLD_NOW | RTLD_GLOBAL);
    if (!h) {
        fprintf(stderr, "[driver] dlopen failed: %s\n", dlerror());
        return 1;
    }
    fprintf(stderr, "[driver] dlopen ok: handle=%p\n", h);

    auto create_agent        = resolve_or_warn<create_agent_fn>(h,        "bambu_network_create_agent");
    auto set_config_dir_fn_  = resolve_or_warn<set_config_dir_fn>(h,      "bambu_network_set_config_dir");
    auto change_user         = resolve_or_warn<change_user_fn>(h,         "bambu_network_change_user");
    auto install_device_cert = resolve_or_warn<install_device_cert_fn>(h, "bambu_network_install_device_cert");
    auto set_country_code    = resolve_or_warn<set_country_code_fn>(h,    "bambu_network_set_country_code");
    auto network_start       = resolve_or_warn<start_fn>(h,               "bambu_network_start");
    auto init_log            = resolve_or_warn<init_log_fn>(h,            "bambu_network_init_log");
    auto is_user_login       = resolve_or_warn<is_user_login_fn>(h,       "bambu_network_is_user_login");
    auto get_version         = resolve_or_warn<get_version_fn>(h,         "bambu_network_get_version");

    if (!create_agent || !change_user || !install_device_cert) {
        fprintf(stderr, "[driver] missing required symbol(s); abort\n");
        return 1;
    }

    if (get_version) {
        std::string v = get_version();
        fprintf(stderr, "[driver] plugin version: %s\n", v.c_str());
    }

    void* agent = create_agent(std::string(log_dir));
    fprintf(stderr, "[driver] create_agent => agent=%p\n", agent);
    if (!agent) return 1;

    if (set_config_dir_fn_) {
        int rc = set_config_dir_fn_(agent, std::string(config_dir));
        fprintf(stderr, "[driver] set_config_dir(%s) = %d\n", config_dir, rc);
    }
    if (init_log) {
        int rc = init_log(agent);
        fprintf(stderr, "[driver] init_log = %d\n", rc);
    }
    if (set_country_code) {
        int rc = set_country_code(agent, std::string("us"));
        fprintf(stderr, "[driver] set_country_code(us) = %d\n", rc);
    }
    if (network_start) {
        int rc = network_start(agent);
        fprintf(stderr, "[driver] network_start = %d\n", rc);
    }

    // Build user_info JSON. The shape mirrors what BS dumps via
    // j.dump() in GUI_App.cpp:4856 — basically the cloud-login JSON we
    // already have in ~/.x2d/cloud_session.json.
    char user_info[16384];
    snprintf(user_info, sizeof(user_info),
        "{\"access_token\":\"%s\","
        "\"refresh_token\":\"%s\","
        "\"user_id\":\"%s\","
        "\"region\":\"us\","
        "\"expires_in\":31536000}",
        token, token, user_id);
    int rc = change_user(agent, std::string(user_info));
    fprintf(stderr, "[driver] change_user = %d\n", rc);
    if (is_user_login) {
        bool li = is_user_login(agent);
        fprintf(stderr, "[driver] is_user_login = %s\n", li ? "true" : "false");
    }

    fprintf(stderr, "[driver] install_device_cert(dev_id=%s, lan_only=%d) ...\n",
            dev_id, lan_only);
    install_device_cert(agent, std::string(dev_id), lan_only);
    fprintf(stderr, "[driver] install_device_cert RETURNED\n");

    // Sleep so the agent's worker thread completes any async cert-install
    // work (network, AES decrypt, file write).
    fprintf(stderr, "[driver] sleeping 30s for async work to complete...\n");
    sleep(30);

    fprintf(stderr, "[driver] done. inspect %s for any cert/key files written.\n",
            config_dir);
    return 0;
}
