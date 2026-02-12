class StockPrice < Formula
  include Language::Python::Virtualenv

  desc "CLI to track stock prices and dividends"
  homepage "https://github.com/jonathan/stock_price"
  
  # When published, update this URL to your GitHub release tag
  url "https://github.com/jonathan/stock_price/archive/refs/tags/v0.1.1.tar.gz"
  sha256 "REPLACE_WITH_ACTUAL_SHA256" # Get this via 'shasum -a 256' on the release tarball

  depends_on "python@3.12"

  def install
    venv = virtualenv_create(libexec, "python3.12")
    venv.pip_install_and_link buildpath
  end

  test do
    system "#{bin}/stock-price", "--help"
  end
end
