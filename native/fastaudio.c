#define PY_SSIZE_T_CLEAN
#include <Python.h>

#define MINIAUDIO_IMPLEMENTATION
#define MA_NO_ENCODING
#define MA_NO_FLAC
#define MA_NO_MP3
#define MA_NO_GENERATION
#define MA_ENABLE_ONLY_SPECIFIC_BACKENDS
#define MA_ENABLE_WASAPI
#include "third_party/miniaudio/miniaudio.h"

#define WAKE_MS 600
#define KEEP_ALIVE_HZ 20
#define KEEP_ALIVE_LEVEL 0.0056f

typedef struct Sound {
    ma_sound sound;
    struct Sound *prev, *next;
} Sound;

static ma_device device;
static ma_engine engine;
static int ready;
static Sound *sounds;
static ma_uint64 woke_at;
static ULONGLONG busy_tick;
static double keep_alive_phase;

static void render(ma_device *output, void *frames_out, const void *frames_in, ma_uint32 frame_count)
{
    float *samples = (float *)frames_out;
    ma_uint32 channels = output->playback.channels;
    double step = MA_TAU_D * KEEP_ALIVE_HZ / output->sampleRate;
    (void)frames_in;
    ma_engine_read_pcm_frames(&engine, samples, frame_count, NULL);
    for (ma_uint32 frame = 0; frame < frame_count; ++frame) {
        float tone = KEEP_ALIVE_LEVEL * (float)sin(keep_alive_phase);
        keep_alive_phase = fmod(keep_alive_phase + step, MA_TAU_D);
        for (ma_uint32 channel = 0; channel < channels; ++channel) {
            samples[frame * channels + channel] += tone;
        }
    }
}

static int prepare(void)
{
    ma_device_config config;
    ma_engine_config engine_config;
    if (ready) {
        return 1;
    }
    config = ma_device_config_init(ma_device_type_playback);
    config.playback.format = ma_format_f32;
    config.dataCallback = render;
    if (ma_device_init(NULL, &config, &device) != MA_SUCCESS) {
        return 0;
    }
    engine_config = ma_engine_config_init();
    engine_config.noDevice = MA_TRUE;
    engine_config.channels = device.playback.channels;
    engine_config.sampleRate = device.sampleRate;
    if (ma_engine_init(&engine_config, &engine) != MA_SUCCESS) {
        ma_device_uninit(&device);
        return 0;
    }
    ready = 1;
    return 1;
}

static int start_output(void)
{
    if (!ma_device_is_started(&device)) {
        if (ma_device_start(&device) != MA_SUCCESS) {
            return 0;
        }
        woke_at = ma_engine_get_time_in_pcm_frames(&engine);
    }
    busy_tick = GetTickCount64();
    return 1;
}

static Sound *sound_of(PyObject *capsule)
{
    return (Sound *)PyCapsule_GetPointer(capsule, "fastaudio.Sound");
}

static int started(Sound *entry)
{
    return ma_node_get_state(&entry->sound) == ma_node_state_started;
}

static ma_uint32 rate_of(Sound *entry)
{
    ma_uint32 rate = 0;
    ma_sound_get_data_format(&entry->sound, NULL, NULL, &rate, NULL, 0);
    return rate;
}

static void release(PyObject *capsule)
{
    Sound *entry = sound_of(capsule);
    if (entry->prev) {
        entry->prev->next = entry->next;
    } else {
        sounds = entry->next;
    }
    if (entry->next) {
        entry->next->prev = entry->prev;
    }
    ma_sound_uninit(&entry->sound);
    PyMem_RawFree(entry);
}

static PyObject *available(PyObject *module, PyObject *unused)
{
    return PyBool_FromLong(prepare());
}

static PyObject *load(PyObject *module, PyObject *path)
{
    wchar_t *wide;
    Sound *entry;
    ma_result result;
    PyObject *capsule;
    if (!prepare()) {
        return PyErr_Format(PyExc_OSError, "no audio output device could be opened");
    }
    wide = PyUnicode_AsWideCharString(path, NULL);
    if (wide == NULL) {
        return NULL;
    }
    entry = (Sound *)PyMem_RawCalloc(1, sizeof(Sound));
    if (entry == NULL) {
        PyMem_Free(wide);
        return PyErr_NoMemory();
    }
    Py_BEGIN_ALLOW_THREADS
    result = ma_sound_init_from_file_w(&engine, wide, MA_SOUND_FLAG_DECODE | MA_SOUND_FLAG_NO_SPATIALIZATION, NULL, NULL,
                                       &entry->sound);
    Py_END_ALLOW_THREADS
    PyMem_Free(wide);
    if (result != MA_SUCCESS) {
        PyMem_RawFree(entry);
        return PyErr_Format(PyExc_OSError, "could not load %U: %s", path, ma_result_description(result));
    }
    capsule = PyCapsule_New(entry, "fastaudio.Sound", release);
    if (capsule == NULL) {
        ma_sound_uninit(&entry->sound);
        PyMem_RawFree(entry);
        return NULL;
    }
    entry->next = sounds;
    if (sounds) {
        sounds->prev = entry;
    }
    sounds = entry;
    return capsule;
}

static PyObject *play(PyObject *module, PyObject *args)
{
    PyObject *capsule;
    int hold = 0;
    Sound *entry;
    ma_uint64 now, earliest;
    if (!PyArg_ParseTuple(args, "O|p", &capsule, &hold) || (entry = sound_of(capsule)) == NULL) {
        return NULL;
    }
    if (!start_output()) {
        return PyErr_Format(PyExc_OSError, "the audio output could not start");
    }
    now = ma_engine_get_time_in_pcm_frames(&engine);
    earliest = hold ? woke_at + (ma_uint64)device.sampleRate * WAKE_MS / 1000 : 0;
    ma_sound_set_start_time_in_pcm_frames(&entry->sound, now < earliest ? earliest : 0);
    if (ma_sound_at_end(&entry->sound)) {
        ma_sound_seek_to_pcm_frame(&entry->sound, 0);
    }
    ma_sound_start(&entry->sound);
    Py_RETURN_NONE;
}

static PyObject *wake(PyObject *module, PyObject *unused)
{
    return PyBool_FromLong(prepare() && start_output());
}

static PyObject *pause(PyObject *module, PyObject *capsule)
{
    Sound *entry = sound_of(capsule);
    if (entry == NULL) {
        return NULL;
    }
    ma_sound_stop(&entry->sound);
    Py_RETURN_NONE;
}

static PyObject *seek(PyObject *module, PyObject *args)
{
    PyObject *capsule;
    double ms;
    Sound *entry;
    if (!PyArg_ParseTuple(args, "Od", &capsule, &ms) || (entry = sound_of(capsule)) == NULL) {
        return NULL;
    }
    ma_sound_seek_to_pcm_frame(&entry->sound, (ma_uint64)(ms > 0 ? ms * rate_of(entry) / 1000 : 0));
    Py_RETURN_NONE;
}

static PyObject *position(PyObject *module, PyObject *capsule)
{
    Sound *entry = sound_of(capsule);
    ma_uint64 cursor = 0;
    if (entry == NULL) {
        return NULL;
    }
    ma_sound_get_cursor_in_pcm_frames(&entry->sound, &cursor);
    return PyFloat_FromDouble((double)cursor * 1000 / rate_of(entry));
}

static PyObject *duration(PyObject *module, PyObject *capsule)
{
    Sound *entry = sound_of(capsule);
    ma_uint64 length = 0;
    if (entry == NULL) {
        return NULL;
    }
    ma_sound_get_length_in_pcm_frames(&entry->sound, &length);
    return PyFloat_FromDouble((double)length * 1000 / rate_of(entry));
}

static PyObject *playing(PyObject *module, PyObject *capsule)
{
    Sound *entry = sound_of(capsule);
    if (entry == NULL) {
        return NULL;
    }
    return PyBool_FromLong(started(entry));
}

static PyObject *configure(PyObject *module, PyObject *args)
{
    PyObject *capsule;
    float volume, pitch;
    int looping;
    Sound *entry;
    if (!PyArg_ParseTuple(args, "Offp", &capsule, &volume, &pitch, &looping) || (entry = sound_of(capsule)) == NULL) {
        return NULL;
    }
    ma_sound_set_volume(&entry->sound, volume);
    ma_sound_set_pitch(&entry->sound, pitch);
    ma_sound_set_looping(&entry->sound, looping);
    Py_RETURN_NONE;
}

static PyObject *suspend_if_idle(PyObject *module, PyObject *arg)
{
    double seconds = PyFloat_AsDouble(arg);
    if (seconds == -1.0 && PyErr_Occurred()) {
        return NULL;
    }
    if (!ready || !ma_device_is_started(&device)) {
        Py_RETURN_FALSE;
    }
    for (Sound *entry = sounds; entry; entry = entry->next) {
        if (started(entry)) {
            busy_tick = GetTickCount64();
            Py_RETURN_FALSE;
        }
    }
    if (GetTickCount64() - busy_tick < seconds * 1000) {
        Py_RETURN_FALSE;
    }
    Py_BEGIN_ALLOW_THREADS
    ma_device_stop(&device);
    Py_END_ALLOW_THREADS
    Py_RETURN_TRUE;
}

static PyMethodDef methods[] = {
    {"available", available, METH_NOARGS, NULL},
    {"load", load, METH_O, NULL},
    {"play", play, METH_VARARGS, NULL},
    {"wake", wake, METH_NOARGS, NULL},
    {"pause", pause, METH_O, NULL},
    {"seek", seek, METH_VARARGS, NULL},
    {"position", position, METH_O, NULL},
    {"duration", duration, METH_O, NULL},
    {"playing", playing, METH_O, NULL},
    {"configure", configure, METH_VARARGS, NULL},
    {"suspend_if_idle", suspend_if_idle, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module_def = {PyModuleDef_HEAD_INIT, "fastaudio", NULL, -1, methods};

PyMODINIT_FUNC PyInit_fastaudio(void)
{
    return PyModule_Create(&module_def);
}
