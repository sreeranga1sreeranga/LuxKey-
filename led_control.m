// led_control.m
#import <Foundation/Foundation.h>
#import <IOKit/hid/IOHIDManager.h>
#import <IOKit/hid/IOHIDUsageTables.h>

void setCapsLockLED(bool state) {
    IOHIDManagerRef manager = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
    NSDictionary *match = @{
        @(kIOHIDDeviceUsagePageKey): @(kHIDPage_GenericDesktop),
        @(kIOHIDDeviceUsageKey): @(kHIDUsage_GD_Keyboard)
    };
    IOHIDManagerSetDeviceMatching(manager, (__bridge CFDictionaryRef)match);
    IOHIDManagerOpen(manager, kIOHIDOptionsTypeNone);

    CFSetRef devices = IOHIDManagerCopyDevices(manager);
    if (!devices) return;

    CFIndex count = CFSetGetCount(devices);
    IOHIDDeviceRef *deviceRefs = malloc(sizeof(IOHIDDeviceRef) * count);
    CFSetGetValues(devices, (const void **)deviceRefs);

    for (CFIndex i = 0; i < count; i++) {
        IOHIDDeviceRef device = deviceRefs[i];
        CFArrayRef elements = IOHIDDeviceCopyMatchingElements(device, NULL, kIOHIDOptionsTypeNone);
        if (!elements) continue;

        for (CFIndex j = 0; j < CFArrayGetCount(elements); j++) {
            IOHIDElementRef element = (IOHIDElementRef)CFArrayGetValueAtIndex(elements, j);
            if (IOHIDElementGetUsagePage(element) == kHIDPage_LEDs &&
                IOHIDElementGetUsage(element) == kHIDUsage_LED_CapsLock) {
                IOHIDValueRef val = IOHIDValueCreateWithIntegerValue(kCFAllocatorDefault, element, 0, state ? 1 : 0);
                IOHIDDeviceSetValue(device, element, val);
                CFRelease(val);
            }
        }
        CFRelease(elements);
    }
    free(deviceRefs);
    CFRelease(devices);
    IOHIDManagerClose(manager, kIOHIDOptionsTypeNone);
    CFRelease(manager);
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        if (argc < 2) return 1;
        int blinks = atoi(argv[1]);
        int duration_ms = (argc >= 3) ? atoi(argv[2]) : 350;

        for (int i = 0; i < blinks; i++) {
            setCapsLockLED(true);
            usleep(duration_ms * 1000);
            setCapsLockLED(false);
            usleep(duration_ms * 1000);
        }
    }
    return 0;
}