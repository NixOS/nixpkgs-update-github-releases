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
            pydantic
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

      checks = builtins.mapAttrs (system: pkgs: {
        ruff-format =
          pkgs.runCommand "ruff-format-check"
            {
              nativeBuildInputs = [ pkgs.ruff ];
            }
            ''
              cd ${./.}
              ruff format --check --no-cache .
              touch $out
            '';

        ruff-check =
          pkgs.runCommand "ruff-check"
            {
              nativeBuildInputs = [ pkgs.ruff ];
            }
            ''
              cd ${./.}
              ruff check --no-cache .
              touch $out
            '';

        pyright =
          pkgs.runCommand "pyright"
            {
              nativeBuildInputs = [
                pkgs.pyright
                inputs.self.packages.${system}.nixpkgs-update-github-releases
              ];
            }
            ''
              cd ${./.}
              pyright .
              touch $out
            '';
      }) inputs.nixpkgs.legacyPackages;

      devShells = builtins.mapAttrs (system: pkgs: {
        default = pkgs.mkShell {
          inputsFrom = [ inputs.self.packages.${system}.nixpkgs-update-github-releases ];
          packages = with pkgs; [
            ruff
            pyright
          ];
        };
      }) inputs.nixpkgs.legacyPackages;
    };
}
