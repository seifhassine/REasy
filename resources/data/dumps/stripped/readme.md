# stripper_propagator.py

Use this tool when you want to make any changes to the RSZ dumps (in parent directory).

## When to use this tool

- Title Updates: Use Praydog's dumper to dump the RSZ template (don't forget to use the include_parents flag in the non_native_dumper), then use this tool to strip it, then patch it with the rsz[gamename]_strip.json using the `rsz_template_patcher` script in `/tools`, then propagate the result to obtain the final RSZ dump in the `dumps` folder. 
- RSZ Dump improvements: If you want to identify more fields or make corrections, always make your changes in `rsz[gamename]_strip.json` (NOT `rsz[gamename].json`), then propagate and commit both the stripped json and the full json.

## Commands


### Strip
For each type, remove all fields belonging to the parent(s), leaving each type with no inherited fields.
```bash
python stripper_propagator.py strip "rsz[gamename].json"
```

### Propagate
For each type, append all fields belonging to the parent(s) at the start. The result will be saved in the parent directory (`dumps` in this case.)
```bash
python stripper_propagator.py propagate "rsz[gamename]_strip.json"
```
