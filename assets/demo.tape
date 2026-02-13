# Output file
Output assets/demo.gif

# Settings
Set FontSize 20
Set Width 1200
Set Height 600
Set Padding 40
Set FontFamily "DejaVu Sans Mono"
Set WindowBar Colorful

# Setup the environment (hidden)
Hide
Type "export PYTHONPATH=$PYTHONPATH:." Enter
Type "cat <<EOF > demo_config.yaml" Enter
Type "holdings:" Enter
Type "  AAPL: 10" Enter
Type "  SVOL-B.ST: 100" Enter
Type "  MC.PA: 5" Enter
Type "  INDU-C.ST: 21" Enter
Type "currency: EUR" Enter
Type "EOF" Enter
Type "export STOCK_PRICE_CONFIG=demo_config.yaml" Enter
Type "clear" Enter
Show

# Start the Demo
Type "stock-price --watch" Sleep 500ms Enter
Sleep 10s

# Exit
Ctrl+C
Sleep 2s

# Cleanup (hidden)
Hide
Type "rm demo_config.yaml" Enter
