[StagingDefinition = [Kind = "FastCopy"]]
section Section1;

shared fx_rates = let
    Source = SharePoint.Files("https://contoso.sharepoint.com/sites/finance", [ApiVersion = 15]),
    File = Source{[Name = "fx_rates.xlsx"]}[Content],
    Sheet = Excel.Workbook(File){[Item = "Rates"]}[Data],
    Promoted = Table.PromoteHeaders(Sheet),
    Typed = Table.TransformColumnTypes(Promoted, {{"Date", type date}, {"Currency", type text}, {"RateToEUR", type number}}),
    Renamed = Table.RenameColumns(Typed, {{"Date", "rate_date"}, {"Currency", "currency"}, {"RateToEUR", "rate_to_eur"}})
in
    Renamed;
