# load gz lib
import ordered_set


def function_e():
    print("Called from core.module_e, in folder")
    print(f"loaded ordered_set, version: {ordered_set.__version__}")
