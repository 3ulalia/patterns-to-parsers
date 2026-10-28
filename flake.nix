{
  description = "Uses Python and Vitis HLS to compile data-throughput frontends into RTL";

  outputs =
    {
      self,
      pkgs,
    }:
    {
      devShell.x86_64-linux = pkgs.mkShell {
        packages = with pkgs; [
          python3
          ty
          ruff
          pythonPackages.numpy
          pythonPackages.pytest
        ];
      };
    };
}
