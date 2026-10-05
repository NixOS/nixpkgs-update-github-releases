with import ./. {};

pkgs.mkShell {
  buildInputs = [
    interpreter
  ];
}
