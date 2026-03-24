#include "viper_rt.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// -- string operations --

viper_str* viper_str_new(const char* s, int64_t len) {
    viper_str* str = (viper_str*)malloc(sizeof(viper_str) + len + 1);
    if (!str) {
        fprintf(stderr, "viper: out of memory\n");
        exit(1);
    }
    str->header.refcount = 1;
    str->header.type_id = VIPER_TYPE_STR;
    str->len = len;
    memcpy(str->data, s, len);
    str->data[len] = '\0';
    return str;
}

// string literals are heap-allocated with refcount pinned so they never free
viper_str* viper_str_lit(const char* s) {
    int64_t len = (int64_t)strlen(s);
    viper_str* str = viper_str_new(s, len);
    // pin refcount so literals are never freed
    str->header.refcount = INT64_MAX;
    return str;
}

viper_str* viper_str_concat(viper_str* a, viper_str* b) {
    int64_t new_len = a->len + b->len;
    viper_str* str = (viper_str*)malloc(sizeof(viper_str) + new_len + 1);
    if (!str) {
        fprintf(stderr, "viper: out of memory\n");
        exit(1);
    }
    str->header.refcount = 1;
    str->header.type_id = VIPER_TYPE_STR;
    str->len = new_len;
    memcpy(str->data, a->data, a->len);
    memcpy(str->data + a->len, b->data, b->len);
    str->data[new_len] = '\0';
    return str;
}

// -- print functions --

void viper_print_str(viper_str* s) {
    fwrite(s->data, 1, s->len, stdout);
    putchar('\n');
}

void viper_print_int(int64_t v) {
    printf("%lld\n", (long long)v);
}

void viper_print_float(double v) {
    printf("%g\n", v);
}

void viper_print_bool(int8_t v) {
    printf("%s\n", v ? "True" : "False");
}

// -- refcount --

void viper_incref(void* obj) {
    if (!obj) return;
    viper_obj_header* h = (viper_obj_header*)obj;
    if (h->refcount < INT64_MAX) {
        h->refcount++;
    }
}

void viper_decref(void* obj) {
    if (!obj) return;
    viper_obj_header* h = (viper_obj_header*)obj;
    if (h->refcount < INT64_MAX) {
        h->refcount--;
        if (h->refcount <= 0) {
            free(obj);
        }
    }
}

// -- runtime lifecycle --

void viper_runtime_init(void) {
    // placeholder for future initialization (arena allocator, gc roots, etc)
}

void viper_runtime_cleanup(void) {
    // placeholder for future cleanup
}
