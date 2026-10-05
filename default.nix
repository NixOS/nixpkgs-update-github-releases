{ pkgs ? import <nixpkgs> {} }:

with pkgs;
rec {
  inherit pkgs;

  interpreter = python3.withPackages (p: with p;
    [requests python-dateutil libversion cachecontrol lockfile filelock]
  );
}
