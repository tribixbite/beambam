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
