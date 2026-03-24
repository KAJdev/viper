#ifndef VIPER_RT_H
#define VIPER_RT_H

#include <stdint.h>
#include <stddef.h>

// reference-counted object header, embedded at the start of every heap object
typedef struct {
    int64_t refcount;
    uint32_t type_id;
} viper_obj_header;

// type ids for built-in types
enum {
    VIPER_TYPE_STR = 1,
    VIPER_TYPE_LIST = 2,
    VIPER_TYPE_DICT = 3,
};

// string representation: length-prefixed, utf-8, refcounted
typedef struct {
    viper_obj_header header;
    int64_t len;
    char data[];  // flexible array member
} viper_str;

// create a string from a c literal (borrows, does not copy for static strings)
viper_str* viper_str_lit(const char* s);

// create a string by copying n bytes
viper_str* viper_str_new(const char* s, int64_t len);

// concatenate two strings
viper_str* viper_str_concat(viper_str* a, viper_str* b);

// print a string to stdout followed by newline
void viper_print_str(viper_str* s);

// print an int to stdout followed by newline
void viper_print_int(int64_t v);

// print a float to stdout followed by newline
void viper_print_float(double v);

// print a bool to stdout followed by newline
void viper_print_bool(int8_t v);

// refcount operations
void viper_incref(void* obj);
void viper_decref(void* obj);

// runtime lifecycle
void viper_runtime_init(void);
void viper_runtime_cleanup(void);

#endif
