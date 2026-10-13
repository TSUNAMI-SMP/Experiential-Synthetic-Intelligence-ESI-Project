"""Load the native MOC3 in a disposable process before the GUI uses it."""

import sys


def main():
    import live2d

    live2d.init()
    live2d.enableLog(False)
    model = live2d.Model()
    model.LoadModelJson(sys.argv[1], create_renderer=False)
    if not model.IsV3() or model.GetParamCount() < 1:
        return 1
    del model
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
