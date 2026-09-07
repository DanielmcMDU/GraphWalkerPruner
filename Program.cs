using System.Diagnostics;
using System.Windows.Forms;

namespace GraphWalkerPrunerLauncher;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();

        string baseDirectory = AppContext.BaseDirectory;
        string guiScript = Path.Combine(baseDirectory, "pruner_gui.py");

        if (!File.Exists(guiScript))
        {
            MessageBox.Show(
                $"Could not find the GUI script:\n{guiScript}",
                "GraphWalker Pruner",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return;
        }

        var candidates = new[]
        {
            new PythonCandidate("pythonw.exe", ""),
            new PythonCandidate("python.exe", ""),
            new PythonCandidate("pyw.exe", "-3"),
            new PythonCandidate("py.exe", "-3")
        };

        foreach (var candidate in candidates)
        {
            try
            {
                var startInfo = new ProcessStartInfo
                {
                    FileName = candidate.Executable,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WorkingDirectory = baseDirectory
                };

                if (!string.IsNullOrWhiteSpace(candidate.PrefixArguments))
                {
                    startInfo.ArgumentList.Add(candidate.PrefixArguments);
                }
                startInfo.ArgumentList.Add(guiScript);

                using Process? process = Process.Start(startInfo);
                if (process is null)
                {
                    continue;
                }

                process.WaitForExit();
                Environment.ExitCode = process.ExitCode;
                return;
            }
            catch (System.ComponentModel.Win32Exception)
            {
                // Candidate was not found. Try the next Python launcher.
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    $"Python was found, but the GraphWalker Pruner could not be started.\n\n{ex.Message}",
                    "GraphWalker Pruner",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
                return;
            }
        }

        MessageBox.Show(
            "Python 3 could not be found. Install Python 3 or add python.exe to PATH, then run the solution again.",
            "GraphWalker Pruner",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error);
    }

    private sealed record PythonCandidate(string Executable, string PrefixArguments);
}
