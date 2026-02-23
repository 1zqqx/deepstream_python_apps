
#pragma once


#include "nvdsmeta_schema.h"

typedef struct {
    char* id;
    char* name;
    double confidence;

    int frameId;
    /** source id */
    int sensorid;

    NvDsRect bbox;
    /** Holds a pointer to the generated event's timestamp. */
    char* ts;
    /** Holds a pointer to the detected or inferred object's ID. */
    char* objectId;
    /** Holds a pointer to a string containing the sensor's identity. */
    char* sensorStr;
} CTFaceObjectMeta;
