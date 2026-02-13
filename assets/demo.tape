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
Type "mkdir -p ~/.stock_price.yaml" Enter
Type "cat <<EOF > ~/.stock_price.yaml
holdings:
  AAPL: 10
  SVOL-B.ST: 100
  MC.PA: 5
  INDU-C.ST: 21
currency: EUR
EOF" Enter
Type "clear" Enter
Show

# Start the Demo
Type "python3 stock.py --watch" Sleep 500ms Enter
Sleep 10s

# Exit
Ctrl+C
Sleep 2s
