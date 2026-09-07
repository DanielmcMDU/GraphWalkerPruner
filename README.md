# GraphWalker Pruner GUI — Visual Studio version

Creates a product-specific GraphWalker JSON model by retaining the elements associated with selected `FEATURE=...` annotations. Unannotated elements are kept. The pruner also removes variables owned only by excluded features, simplifies affected guards and synchronizes GraphWalker editor metadata.


## GUI behaviour

- Select a 150% GraphWalker JSON model.
- Select features discovered from `FEATURE = ...` annotations.
- Choose the 100% output model path.
- Run pruning.
- Review completion or error feedback in the status line and dialog box.
- The pruned product-specific model is written to the selected output path.

Feature names are compared without regard to letter case. Multiple names in one annotation form an OR-list, so `FEATURE=A, B` is retained when either feature is selected.

## Command line

The pruning engine can also be run without the GUI:

```text
python prune_graphwalker_model.py input.json config.json output.json
```

Example configuration:

```json
{
  "selected_features": ["FEATURE_A", "FEATURE_B"],
  "strict": true,
  "common_variables": []
}
```

`common_variables` is optional. It can be used to protect variables that are shared by annotated and unannotated behaviour.


## Standalone executable

For a fully standalone Python-packaged executable, run `build_exe.bat`. It uses PyInstaller to create:

`dist\\GraphWalkerPruner.exe`

That packaging route is separate from the Visual Studio C# launcher.
