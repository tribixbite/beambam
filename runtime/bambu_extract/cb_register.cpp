// cb_register.cpp — registers the std::function callbacks the plugin
// expects before connect_server / install_device_cert. The plugin bails
// out of cert-issuance silently if these callbacks aren't wired.
//
// Build (on a host with libstdc++ that matches the plugin's ABI):
//   g++ -O1 -shared -fPIC -std=c++17 cb_register.cpp -ldl -o cb_register.so
//
// Use from dump_driver_c.c:
//   void* cb = dlopen("./cb_register.so", RTLD_NOW);
//   int (*reg)(void*, void*) = dlsym(cb, "register_all_callbacks");
//   reg(libhandle, agent_ptr);

#include <functional>
#include <string>
#include <dlfcn.h>
#include <cstdio>

typedef std::function<void(int online_login, bool login)>                OnUserLoginFn;
typedef std::function<void(std::string topic_str)>                       OnPrinterConnectedFn;
typedef std::function<void(int return_code, int reason_code)>            OnServerConnectedFn;
typedef std::function<void(int status, std::string dev_id, std::string msg)> OnLocalConnectedFn;
typedef std::function<void(std::string dev_id, std::string msg)>         OnMessageFn;

typedef int (*set_login_t)(void*, OnUserLoginFn);
typedef int (*set_server_t)(void*, OnServerConnectedFn);
typedef int (*set_local_conn_t)(void*, OnLocalConnectedFn);
typedef int (*set_msg_t)(void*, OnMessageFn);
typedef int (*set_printer_conn_t)(void*, OnPrinterConnectedFn);
typedef int (*set_http_err_t)(void*, std::function<void(unsigned int, std::string)>);
typedef int (*set_ssdp_t)(void*, std::function<void(std::string)>);
typedef int (*set_country_t)(void*, std::function<std::string()>);
typedef int (*set_sub_fail_t)(void*, std::function<void(std::string)>);
typedef int (*set_queue_t)(void*, std::function<void(std::function<void()>)>);

extern "C" int register_all_callbacks(void* lib, void* agent) {
    auto set_login        = (set_login_t)       dlsym(lib, "bambu_network_set_on_user_login_fn");
    auto set_server       = (set_server_t)      dlsym(lib, "bambu_network_set_on_server_connected_fn");
    auto set_local_conn   = (set_local_conn_t)  dlsym(lib, "bambu_network_set_on_local_connect_fn");
    auto set_msg          = (set_msg_t)         dlsym(lib, "bambu_network_set_on_message_fn");
    auto set_local_msg    = (set_msg_t)         dlsym(lib, "bambu_network_set_on_local_message_fn");
    auto set_user_msg     = (set_msg_t)         dlsym(lib, "bambu_network_set_on_user_message_fn");
    auto set_printer_conn = (set_printer_conn_t)dlsym(lib, "bambu_network_set_on_printer_connected_fn");
    auto set_http_err     = (set_http_err_t)    dlsym(lib, "bambu_network_set_on_http_error_fn");
    auto set_ssdp         = (set_ssdp_t)        dlsym(lib, "bambu_network_set_on_ssdp_msg_fn");

    if (set_login) {
        set_login(agent, [](int online_login, bool login) {
            fprintf(stderr, "[cb] user_login online=%d login=%d\n", online_login, (int)login);
        });
        fprintf(stderr, "[cb-reg] set_on_user_login_fn OK\n");
    }
    if (set_server) {
        set_server(agent, [](int rc, int reason) {
            fprintf(stderr, "[cb] server_connected rc=%d reason=%d\n", rc, reason);
        });
        fprintf(stderr, "[cb-reg] set_on_server_connected_fn OK\n");
    }
    if (set_local_conn) {
        set_local_conn(agent, [](int status, std::string dev_id, std::string msg) {
            fprintf(stderr, "[cb] local_connect status=%d dev=%s msg=%.120s\n",
                    status, dev_id.c_str(), msg.c_str());
        });
        fprintf(stderr, "[cb-reg] set_on_local_connect_fn OK\n");
    }
    if (set_msg) {
        set_msg(agent, [](std::string dev_id, std::string msg) {
            fprintf(stderr, "[cb] message dev=%s msg=%.120s\n", dev_id.c_str(), msg.c_str());
        });
        fprintf(stderr, "[cb-reg] set_on_message_fn OK\n");
    }
    if (set_local_msg) {
        set_local_msg(agent, [](std::string dev_id, std::string msg) {
            fprintf(stderr, "[cb] local_msg dev=%s msg=%.120s\n", dev_id.c_str(), msg.c_str());
        });
        fprintf(stderr, "[cb-reg] set_on_local_message_fn OK\n");
    }
    if (set_user_msg) {
        set_user_msg(agent, [](std::string dev_id, std::string msg) {
            fprintf(stderr, "[cb] user_msg dev=%s msg=%.120s\n", dev_id.c_str(), msg.c_str());
        });
        fprintf(stderr, "[cb-reg] set_on_user_message_fn OK\n");
    }
    if (set_printer_conn) {
        set_printer_conn(agent, [](std::string topic) {
            fprintf(stderr, "[cb] printer_connected topic=%s\n", topic.c_str());
        });
        fprintf(stderr, "[cb-reg] set_on_printer_connected_fn OK\n");
    }
    if (set_http_err) {
        set_http_err(agent, [](unsigned int code, std::string url) {
            fprintf(stderr, "[cb] http_error code=%u url=%s\n", code, url.c_str());
        });
        fprintf(stderr, "[cb-reg] set_on_http_error_fn OK\n");
    }
    if (set_ssdp) {
        set_ssdp(agent, [](std::string msg) {
            fprintf(stderr, "[cb] ssdp msg=%.80s\n", msg.c_str());
        });
        fprintf(stderr, "[cb-reg] set_on_ssdp_msg_fn OK\n");
    }

    auto set_country = (set_country_t)  dlsym(lib, "bambu_network_set_get_country_code_fn");
    auto set_subfail = (set_sub_fail_t) dlsym(lib, "bambu_network_set_on_subscribe_failure_fn");
    auto set_queue   = (set_queue_t)    dlsym(lib, "bambu_network_set_queue_on_main_fn");
    if (set_country) {
        set_country(agent, []() -> std::string { return std::string("us"); });
        fprintf(stderr, "[cb-reg] set_get_country_code_fn OK\n");
    }
    if (set_subfail) {
        set_subfail(agent, [](std::string dev_id) {
            fprintf(stderr, "[cb] subscribe_failure dev=%s\n", dev_id.c_str());
        });
        fprintf(stderr, "[cb-reg] set_on_subscribe_failure_fn OK\n");
    }
    if (set_queue) {
        // Run on main thread — we don't have a wx event loop, so just invoke inline.
        set_queue(agent, [](std::function<void()> work) {
            try { work(); } catch (...) {
                fprintf(stderr, "[cb] queue_on_main work threw\n");
            }
        });
        fprintf(stderr, "[cb-reg] set_queue_on_main_fn OK\n");
    }
    return 0;
}

// ─── PrintParams mirror + start_print wrapper ────────────────────────────────
//
// PrintParams in BS bambu_networking.hpp is a POD (29 string/bool/int
// fields, no methods, no virtuals, no non-default constructors).
// libstdc++ std::string is 32 bytes; we keep the same field order +
// types to match the plugin's expected layout exactly.

struct PrintParams {
    std::string dev_id;
    std::string task_name;
    std::string project_name;
    std::string preset_name;
    std::string filename;
    std::string config_filename;
    int         plate_index;
    std::string ftp_folder;
    std::string ftp_file;
    std::string ftp_file_md5;
    std::string nozzle_mapping;
    std::string ams_mapping;
    std::string ams_mapping2;
    std::string ams_mapping_info;
    std::string nozzles_info;
    std::string connection_type;
    std::string comments;
    int         origin_profile_id = 0;
    int         stl_design_id = 0;
    std::string origin_model_id;
    std::string print_type;
    std::string dst_file;
    std::string dev_name;
    std::string dev_ip;
    bool        use_ssl_for_ftp;
    bool        use_ssl_for_mqtt;
    std::string username;
    std::string password;
    bool        task_bed_leveling;
    bool        task_flow_cali;
    bool        task_vibration_cali;
    bool        task_layer_inspect;
    bool        task_record_timelapse;
    bool        task_timelapse_use_internal;
    bool        task_use_ams;
    std::string task_bed_type;
    std::string extra_options;
    int         auto_bed_leveling{ 0 };
    int         auto_flow_cali{ 0 };
    int         auto_offset_cali{ 0 };
    int         extruder_cali_manual_mode{ -1 };
    bool        task_ext_change_assist;
    bool        try_emmc_print;
};

typedef std::function<void(int status, int code, std::string msg)> OnUpdateStatusFn;
typedef std::function<bool()>                                       WasCancelledFn;
typedef std::function<bool(int status, std::string job_info)>       OnWaitFn;

typedef int (*func_start_print)(void* agent, PrintParams params,
                                OnUpdateStatusFn update_fn,
                                WasCancelledFn cancel_fn,
                                OnWaitFn wait_fn);
typedef int (*func_start_local_print)(void* agent, PrintParams params,
                                      OnUpdateStatusFn update_fn,
                                      WasCancelledFn cancel_fn,
                                      OnWaitFn wait_fn);
typedef int (*func_start_send_gcode_to_sdcard)(void* agent, PrintParams params,
                                               OnUpdateStatusFn update_fn,
                                               WasCancelledFn cancel_fn,
                                               OnWaitFn wait_fn);

// Plain-C input struct so dump_driver_c.c can populate without C++ ABI knowledge.
struct StartPrintInputs {
    // identity / routing
    const char* dev_id;
    const char* dev_ip;
    const char* dev_name;
    const char* username;
    const char* password;
    const char* connection_type;   // "lan" or "cloud"
    // file
    const char* filename;           // local path to .gcode.3mf
    const char* config_filename;
    const char* project_name;
    const char* task_name;
    const char* preset_name;
    int         plate_index;
    // ftp upload target
    const char* ftp_folder;
    const char* ftp_file;
    const char* ftp_file_md5;
    // mapping (optional)
    const char* ams_mapping;
    const char* ams_mapping2;
    const char* nozzle_mapping;
    const char* task_bed_type;
    // bools
    int task_use_ams;
    int task_bed_leveling;
    int task_flow_cali;
    int task_vibration_cali;
    int task_layer_inspect;
    int task_record_timelapse;
    int use_ssl_for_ftp;
    int use_ssl_for_mqtt;
};

extern "C" int do_start_print(void* lib, void* agent,
                              const StartPrintInputs* in,
                              const char* route /* "lan" / "cloud" / "sdcard" */)
{
    auto resolve = [](const char* s) -> std::string {
        return s ? std::string(s) : std::string();
    };
    PrintParams p{};
    p.dev_id          = resolve(in->dev_id);
    p.task_name       = resolve(in->task_name);
    p.project_name    = resolve(in->project_name);
    p.preset_name     = resolve(in->preset_name);
    p.filename        = resolve(in->filename);
    p.config_filename = resolve(in->config_filename);
    p.plate_index     = in->plate_index;
    p.ftp_folder      = resolve(in->ftp_folder);
    p.ftp_file        = resolve(in->ftp_file);
    p.ftp_file_md5    = resolve(in->ftp_file_md5);
    p.nozzle_mapping  = resolve(in->nozzle_mapping);
    p.ams_mapping     = resolve(in->ams_mapping);
    p.ams_mapping2    = resolve(in->ams_mapping2);
    p.connection_type = resolve(in->connection_type);
    p.dev_name        = resolve(in->dev_name);
    p.dev_ip          = resolve(in->dev_ip);
    p.username        = resolve(in->username);
    p.password        = resolve(in->password);
    p.use_ssl_for_ftp = in->use_ssl_for_ftp != 0;
    p.use_ssl_for_mqtt= in->use_ssl_for_mqtt != 0;
    p.task_bed_leveling     = in->task_bed_leveling != 0;
    p.task_flow_cali        = in->task_flow_cali != 0;
    p.task_vibration_cali   = in->task_vibration_cali != 0;
    p.task_layer_inspect    = in->task_layer_inspect != 0;
    p.task_record_timelapse = in->task_record_timelapse != 0;
    p.task_use_ams          = in->task_use_ams != 0;
    p.task_bed_type         = resolve(in->task_bed_type);
    p.task_timelapse_use_internal = false;
    p.task_ext_change_assist = false;
    p.try_emmc_print = false;

    auto on_update = [](int status, int code, std::string msg) {
        fprintf(stderr, "[start_print] status=%d code=%d msg=%.180s\n",
                status, code, msg.c_str());
    };
    auto was_cancelled = []() -> bool { return false; };
    auto on_wait = [](int status, std::string job) -> bool {
        fprintf(stderr, "[start_print] wait status=%d job=%.120s\n",
                status, job.c_str());
        return true;
    };

    const std::string r = route ? route : "lan";
    if (r == "cloud") {
        auto fn = (func_start_print)dlsym(lib, "bambu_network_start_print");
        if (!fn) { fprintf(stderr, "[start_print] start_print sym missing\n"); return -1; }
        return fn(agent, p, on_update, was_cancelled, on_wait);
    } else if (r == "sdcard") {
        auto fn = (func_start_send_gcode_to_sdcard)dlsym(
            lib, "bambu_network_start_send_gcode_to_sdcard");
        if (!fn) { fprintf(stderr, "[start_print] start_send_gcode_to_sdcard sym missing\n"); return -1; }
        return fn(agent, p, on_update, was_cancelled, on_wait);
    } else {
        // default: lan
        auto fn = (func_start_local_print)dlsym(
            lib, "bambu_network_start_local_print_with_record");
        if (!fn) {
            fn = (func_start_local_print)dlsym(
                lib, "bambu_network_start_local_print");
        }
        if (!fn) { fprintf(stderr, "[start_print] start_local_print* sym missing\n"); return -1; }
        return fn(agent, p, on_update, was_cancelled, on_wait);
    }
}
