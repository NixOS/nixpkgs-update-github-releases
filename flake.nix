{
  inputs = {
    nixpkgs.url = "https://channels.nixos.org/nixpkgs-unstable/nixexprs.tar.zst";

    flake-compat = {
      url = "github:NixOS/flake-compat";
      flake = false;
    };
  };

  outputs =
    inputs:
    let
      package =
        pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = "nixpkgs-update-github-releases";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = with pkgs.python3Packages; [
            hatchling
          ];

          propagatedBuildInputs = with pkgs.python3Packages; [
            cachecontrol
            filelock
            libversion
            lockfile
            python-dateutil
            requests
          ];
        };
    in
    {
      packages = builtins.mapAttrs (system: pkgs: {
        nixpkgs-update-github-releases = package pkgs;
        default = inputs.self.packages.${system}.nixpkgs-update-github-releases;
      }) inputs.nixpkgs.legacyPackages;

      devShells = builtins.mapAttrs (system: pkgs: {
        default = pkgs.mkShell {
          inputsFrom = [ inputs.self.packages.${system}.default ];
        };
      }) inputs.nixpkgs.legacyPackages;
    };
}
