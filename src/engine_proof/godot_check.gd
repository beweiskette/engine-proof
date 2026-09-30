# engine-proof loader check. Run with:
#   godot --headless --path PROJECT -s /abs/path/godot_check.gd -- res://a.gd res://b.tscn
# Loads every given resource without the cache and prints one marker line per path.
extends SceneTree


func _init() -> void:
	var bad := 0
	for p in OS.get_cmdline_user_args():
		var res = ResourceLoader.load(p, "", ResourceLoader.CACHE_MODE_IGNORE)
		if res == null:
			print("EP_LOAD_FAIL ", p)
			bad += 1
		elif res is Script and not res.can_instantiate() \
				and not (res.has_method("is_abstract") and res.is_abstract()):
			print("EP_CANNOT_INSTANTIATE ", p)
			bad += 1
		else:
			print("EP_OK ", p)
	print("EP_DONE")
	quit(1 if bad > 0 else 0)
