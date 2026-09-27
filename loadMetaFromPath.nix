# Not perfect, but it works™️
{ path ? null, url ? null, universeFile }:

assert path == null || url == null;
assert path != null || url != null;

let
  path' = if url == null then path else (builtins.fetchTarball url);
  deepSeqId = x: builtins.deepSeq x x;
  maybeToList = x: if x == null then [] else [x];
  pkgs = import path' {};
  versions = map (
    name:
      let
        path = pkgs.lib.splitString "." name;
        value = pkgs.lib.getAttrFromPath path pkgs;
        result = builtins.tryEval (
          deepSeqId (
            let
              version =
                value.version or
                null;
              pages =
                maybeToList (value.src.meta.homepage or null) ++
                value.src.urls or [] ++
                maybeToList (value.meta.homepage or null) ++
                [];
            in
            assert pkgs.lib.isString version;
            assert pkgs.lib.all pkgs.lib.isString pages;
            {
              inherit name version pages;
            }
          )
        );
      in
        if result.success then result.value else null
    )
    (pkgs.lib.splitString "\n" (pkgs.lib.fileContents universeFile));
in
  pkgs.lib.filter (x: x != null) versions
